"""Run the real Praxis runtime as an external Harbor agent."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from threading import Event

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

from backend.configuration import ClientPaths, LocalConfigStore
from backend.domain import redact_sensitive_text
from backend.providers import ModelConfig
from backend.runtime import RunnerSettings, build_application
from backend.runtime.conversation.trace import conversation_trace_records
from backend.runtime.core.events import RuntimeEvent
from backend.storage.settings import LocalSettingsStore

from .event_collector import EventCollector
from .harbor_tools import HarborTools
from .runner import auto_approve
from .sandbox import Sandbox


class PraxisOptions(AgentOptions):
    config_path: str | None = None
    max_tool_calls: int | None = Field(default=None, ge=1)


class PraxisAgent(BaseAgent):
    options_model = PraxisOptions

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        config_path = self.options.config_path
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.max_tool_calls = self.options.max_tool_calls
        self.stopped = Event()
        self.collector = EventCollector()

    @staticmethod
    def name() -> str:
        return "praxis"

    def version(self) -> str | None:
        root = Path(__file__).resolve().parents[1]
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        return revision + ("-dirty" if dirty else "")

    async def setup(self, environment: BaseEnvironment) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.reasoning_effort = self._reasoning_effort()
        self.selected_model_config = await asyncio.to_thread(self._model_config)
        self.model_name = self.selected_model_config.model
        self._init_model_info()

    def _reasoning_effort(self) -> str:
        path = self.config_path or ClientPaths.from_home().config_file
        section = LocalConfigStore(path).read().get("benchmark", {})
        if not isinstance(section, dict):
            raise ValueError("benchmark must be a TOML table.")
        effort = section.get("reasoning_effort", "max")
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError("benchmark.reasoning_effort must be low, medium, high, xhigh, or max.")
        return effort

    def _model_config(self) -> ModelConfig:
        if self.config_path is not None:
            config = ModelConfig.from_toml(self.config_path)
        else:
            paths = ClientPaths.from_home()
            store = LocalSettingsStore(paths.state_db, paths.config_file)
            try:
                config = store.model_config()
            finally:
                store.close()
        if self.model_name is not None and self.model_name != config.model:
            raise ValueError("Harbor --model must match the selected Praxis model; change Praxis settings explicitly.")
        return config

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        self.stopped.clear()
        self.collector = EventCollector()
        bridge = HarborTools(environment, asyncio.get_running_loop(), self.stopped)
        worker = asyncio.create_task(asyncio.to_thread(self._run_sync, instruction, bridge, context))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            self.stopped.set()
            # Keep the environment alive until the synchronous runtime has exited.
            try:
                await asyncio.shield(worker)
            except Exception:
                self.logger.exception("Praxis worker exited during cancellation")
            raise
        finally:
            self.stopped.set()
            self.populate_context_post_run(context)

    def _run_sync(self, instruction: str, bridge: HarborTools, context: AgentContext) -> None:
        config = self.selected_model_config
        context.metadata = {"model": config.model, "praxis_version": self.version()}
        sandbox = Sandbox(self.logs_dir / "praxis", model_config=config)
        sandbox.prepare()
        workspace = sandbox.workspaces_dir / "task"
        workspace.mkdir(parents=True, exist_ok=False)
        app = build_application(
            workspace,
            planner_name="llm",
            paths=sandbox.paths,
            model_config=config,
            settings=RunnerSettings(max_tool_calls=self.max_tool_calls, log_full_messages=True),
            tools_override=bridge.registry(),
            config_override={name: {"enabled": False} for name in ("memory", "skills", "mcp", "subagents")},
        )
        conversation = None
        try:
            conversation = app.open_conversation()
            conversation.ensure_session()
            conversation.runtime.state.model_snapshot["reasoning_effort"] = self.reasoning_effort
            conversation.runtime.save()

            def collect(event: RuntimeEvent) -> None:
                if event.kind in {"error", "model_error", "context_compaction_failed"}:
                    self.logger.error(
                        "Praxis event: %s",
                        redact_sensitive_text(
                            json.dumps(
                                {
                                    "timestamp": event.timestamp,
                                    "kind": event.kind,
                                    "message": event.message,
                                    "data": event.data,
                                },
                                ensure_ascii=False,
                                default=str,
                            )
                        ),
                    )
                self.collector(event)
                self.populate_context_post_run(context)

            state = conversation.run_task(
                instruction,
                mode="agent",
                on_event=collect,
                interrupt=auto_approve,
                cancel_requested=self.stopped.is_set,
            )
            context.metadata.update(status=state.status, run_id=state.run_id)
            if state.status != "completed" and not self.stopped.is_set():
                detail = redact_sensitive_text(
                    f"Praxis ended with status {state.status!r}; "
                    f"run_id={state.run_id}; stop_reason={state.stop_reason!r}; "
                    f"final_answer={state.final_answer!r}"
                )
                self.logger.error("%s", detail)
                raise RuntimeError(detail)
        finally:
            try:
                if conversation is not None and conversation.runtime is not None:
                    state = conversation.runtime.state
                    with (self.logs_dir / "praxis-trace.jsonl").open("w", encoding="utf-8") as output:
                        for record in conversation_trace_records(app.session_store, state.session_id, state.thread_id):
                            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            finally:
                app.close()

    def populate_context_post_run(self, context: AgentContext) -> None:
        context.n_input_tokens = self.collector.prompt_tokens
        context.n_output_tokens = self.collector.completion_tokens
