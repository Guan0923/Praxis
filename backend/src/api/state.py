"""Shared single-user API runtime state for the local backend."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from backend.configuration import ClientPaths
from backend.domain import AssistantMessage
from backend.domain.execution_config import RuntimeConfigUpdate
from backend.domain.runtime_state import (
    RuntimeState,
    message_payload,
    terminal_error_payload,
    utc_iso,
)
from backend.domain.terminal import TERMINAL_LABELS
from backend.jobs import JobRegistry
from backend.runtime.agent_thread_index import AgentThreadIndex
from backend.runtime.capability_settings import SubagentSettings
from backend.runtime.subagents import SubagentCoordinator
from backend.sandbox import BrokerConfiguration, SandboxMaintenanceGate, WindowsBrokerClient
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.projects import ProjectStore
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream
from backend.storage.settings import LocalSettingsStore
from backend.storage.todo_list import MemoryTodoListStore
from backend.tools.terminal import available_terminal_executables, effective_terminal_type

from .agent_report_projection import project_frame
from .agent_thread_stream import AgentThreadEventHub

DEFAULT_DATA_ROOT = Path.home() / ".praxis"
INTERRUPTED_TURN_MESSAGE = "Turn interrupted because its backend process stopped."


class WebAppState:
    """Process-owned state for one local Praxis installation."""

    def __init__(
        self,
        data_root: Path = DEFAULT_DATA_ROOT,
        *,
        project_picker: Callable[[], Path | None] | None = None,
        job_registry: JobRegistry | None = None,
        sandbox_broker: WindowsBrokerClient | None = None,
        message_queue: MemoryMessageQueue | None = None,
    ) -> None:
        root = Path(data_root)
        if root.is_symlink():
            raise ValueError("Local data root cannot be a symbolic link.")
        self.data_root = root.resolve()
        self.paths = ClientPaths(self.data_root)
        self.paths.ensure()
        self.settings = LocalSettingsStore(self.paths.state_db, self.paths.config_file)
        self.projects = ProjectStore(self.paths.projects_db)
        self.chat_workspace = self.paths.runtime_dir
        self.benchmark_root = self.data_root.parent / ".praxis-cache" / "benchmark"
        self.benchmark_service = None
        self.benchmark_lock = RLock()
        self.project_picker = project_picker
        self.job_registry = job_registry or JobRegistry()
        sandbox_config = self.settings.sandbox_config()
        self.sandbox_broker = sandbox_broker or WindowsBrokerClient.from_system(
            expected_proxy_port=int(sandbox_config["proxy_port"])
        )
        self.sandbox_maintenance = SandboxMaintenanceGate()
        self.sandbox_manifest_path = BrokerConfiguration.create().manifest_path
        self.system_job_scope = self.job_registry.root_scope()
        self.message_queue = message_queue if message_queue is not None else MemoryMessageQueue()
        self.runtime_event_stream = MemoryRuntimeEventStream()
        from .application_sync import ApplicationSync

        self.application_sync = ApplicationSync(self.runtime_event_stream)
        self.projects.on_change = lambda: self.application_sync.publish("catalog.changed")
        self.todo_store = MemoryTodoListStore()
        self.started_at = utc_iso()
        self.closing = False
        self._session_access_lock = RLock()
        self._accessed_sessions: set[str] = set()
        self.mailbox = self.message_queue
        from .terminal_manager import TerminalManager

        self.terminal_manager = TerminalManager()
        from .conversation_cache import ConversationCache

        self.conversation_cache = ConversationCache(self)
        from .conversation_deletion import ConversationDeletion

        self.conversation_deletion = ConversationDeletion(self)
        self.message_queue.on_change = self.conversation_cache.trim
        self.agent_thread_index = AgentThreadIndex()
        self.agent_thread_index.on_store_change = lambda session_id, kind: self.application_sync.publish(
            kind, session_id=session_id
        )

        self.active_runtime_configs: dict[str, RuntimeConfigUpdate] = {}
        self.active_runtime_bridges: dict[str, object] = {}
        self.active_turn_streams: dict[str, object] = {}
        self.active_turn_streams_lock = RLock()
        self.active_runtime_config_locks: dict[str, RLock] = {}
        from .operation_control import OperationControl

        self.operation_control = OperationControl()
        from backend.storage.sqlite import SQLiteSessionStore

        agent_store = SQLiteSessionStore(self.paths, self.agent_thread_index)
        self.session_store = agent_store
        from .runtime_event_transport import publish_frame, publish_terminal

        self.agent_thread_events = AgentThreadEventHub(
            lambda frame, current: project_frame(agent_store, frame, current),
            frame_publisher=lambda frame, current: publish_frame(self, frame, current),
            terminal_publisher=lambda turn: publish_terminal(
                self,
                session_id=turn.session_id,
                thread_id=turn.thread_id,
                turn_id=turn.id,
                terminal_type="success" if turn.status in {"success", "paused"} else "failed",
                message="Agent execution or local persistence failed." if turn.status == "failed" else "",
            ),
        )
        self.subagent_coordinator = SubagentCoordinator(
            settings=SubagentSettings.from_config(self.settings.config_store.read()),
            store=agent_store,
            message_queue=self.message_queue,
            index=self.agent_thread_index,
            job_registry=self.job_registry,
            thread_events=self.agent_thread_events,
        )
        from .turn_message_worker import TurnMessageWorker

        self.turn_message_worker = TurnMessageWorker(self)
        self.turn_message_worker.start()
        from backend.runtime.memory import MemoryAutomationService, MemoryConversationSource, MemoryDiagnosticsRegistry
        from backend.runtime.memory.provider_models import ProviderMemoryModel
        from backend.storage.memory import MemoryStore

        self.memory_store = MemoryStore(self.paths)
        self.memory_source = MemoryConversationSource(self.session_store, self.projects)
        self.memory_diagnostics = MemoryDiagnosticsRegistry()
        self.memory_automation = MemoryAutomationService(self, lambda: ProviderMemoryModel(self.model_config()))
        self.memory_automation.start()

    @staticmethod
    def _delivery_in_node(node: RuntimeState, delivery_id: str) -> bool:
        return any(
            message.get("delivery_id") == delivery_id
            or any(item.get("delivery_id") == delivery_id for item in message.get("content", []))
            for version in node.data
            for message in version
        )

    @staticmethod
    def _fail_interrupted_node(store, node: RuntimeState) -> None:
        error = terminal_error_payload(
            "server",
            INTERRUPTED_TURN_MESSAGE,
            retryable=False,
            code="backend_process_stopped",
        )
        for version in node.data:
            for message in version:
                for item in message["content"]:
                    if item.get("status") == "running":
                        item["status"] = "failed"
            if version[-1]["role"] == "assistant":
                version[-1]["content"].append(error)
            else:
                version.append(message_payload("assistant", [error]))
        node.status = "failed"
        node.timestamp = utc_iso()
        store.finalize_node(RuntimeState.from_dict(node.to_dict()))

    @staticmethod
    def _fail_interrupted_run(store, session_id: str, thread_id: str) -> None:
        run_id = store.running_run_id(session_id, thread_id=thread_id)
        if run_id is None:
            return
        runtime = store.load_runtime(session_id, thread_id=thread_id)
        if runtime is not None and runtime.current_run is not None and runtime.current_run.run_id == run_id:
            run = runtime.current_run
            run.status = "failed"
            assistant = next(
                (
                    message
                    for message in reversed(runtime.messages[run.turn_start_index :])
                    if isinstance(message, AssistantMessage)
                ),
                None,
            )
            if assistant is None:
                runtime.messages.append(AssistantMessage(content=INTERRUPTED_TURN_MESSAGE))
            elif INTERRUPTED_TURN_MESSAGE not in (assistant.content or ""):
                assistant.content = f"{assistant.content}\n\n{INTERRUPTED_TURN_MESSAGE}".strip()
            run.history = runtime.messages
            run.final_answer = INTERRUPTED_TURN_MESSAGE
            runtime.status = "idle"
            runtime.active_message = None
            runtime.active_tool_index = None
            runtime.usage = runtime.turn_usage
            runtime.turn_usage = None
            store.save_runtime(runtime)
        store.finish_turn(session_id, run_id, "failed", INTERRUPTED_TURN_MESSAGE)

    def access_session(self, session_id: str) -> None:
        with self._session_access_lock:
            if session_id in self._accessed_sessions:
                return
            store = self.session_store
            for node in store.load_running_nodes(session_id):
                if isinstance(node, RuntimeState) and node.status == "running" and node.timestamp < self.started_at:
                    self._fail_interrupted_run(store, session_id, node.thread_id)
                    self._fail_interrupted_node(store, node)
            self.agent_thread_index.refresh_session(store, session_id)
            self._accessed_sessions.add(session_id)

    def session_workspace(self, session_id: str) -> Path:
        """Resolve the effective cwd for a session and validate project access."""

        bound = self.projects.session_project(session_id)
        if bound is None:
            self.paths.ensure_session(session_id)
            return self.paths.session_workspace(session_id)
        if bound.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再运行。")
        path = Path(bound.cwd)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            return resolved
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。") from exc

    def copy_session_files(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_files

        copy_session_files(self.paths, source_session_id, target_session_id)

    def copy_session_uploads(self, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_uploads

        copy_session_uploads(self.paths, source_session_id, target_session_id)

    def benchmark_data_root(self) -> Path:
        cache_root = self.benchmark_root.parent
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            raise ValueError("Benchmark cache root cannot be a symbolic link or regular file.")
        if self.benchmark_root.is_symlink() or (self.benchmark_root.exists() and not self.benchmark_root.is_dir()):
            raise ValueError("Benchmark directory must be a regular directory.")
        self.benchmark_root.mkdir(parents=True, exist_ok=True)
        return self.benchmark_root

    def settings_payload(self) -> dict[str, object]:
        result = self.settings.settings()
        if os.name == "nt":
            available = available_terminal_executables(is_windows=True)
            runtime = result.get("runtime_config")
            current = dict(runtime) if isinstance(runtime, dict) else {}
            requested = current.get("terminal_type", "cmd")
            effective = effective_terminal_type(requested, is_windows=True)
            notice: str | None = None
            if not available:
                notice = "未检测到当前系统可用的终端。"
            elif effective != requested:
                notice = "已保存的终端当前不可用，本次已回退到可用终端。"
            current["terminal_type"] = effective
            result["runtime_config"] = current
            result["terminal_options"] = [{"value": name, "label": TERMINAL_LABELS[name]} for name in available]
            result["terminal_notice"] = notice
        else:
            result["terminal_options"] = []
            result["terminal_notice"] = None
        return result

    def model_config(self, provider_name: str | None = None):
        return self.settings.model_config(provider_name)

    def agent_config(self) -> dict[str, object]:
        return self.settings.agent_config()

    def agent_preferences(self) -> str:
        return self.settings.agent_preferences()

    def runtime_config(self) -> dict[str, object]:
        return {"runtime": self.settings.runtime_config()}

    def _finish_active_sessions(self) -> None:
        for session_id in tuple(self._accessed_sessions):
            for node in self.session_store.load_running_nodes(session_id):
                if isinstance(node, RuntimeState) and node.status == "running":
                    self._fail_interrupted_run(self.session_store, session_id, node.thread_id)
                    self._fail_interrupted_node(self.session_store, node)

    def close(self) -> None:
        with self._session_access_lock:
            if self.closing:
                return
            self.closing = True
        actions = [
            self.turn_message_worker.close,
            self.conversation_deletion.close,
            self.subagent_coordinator.close,
            self.message_queue.close,
            self.memory_automation.close,
        ]
        if self.benchmark_service is not None:
            actions.append(self.benchmark_service.close)
        actions.extend(
            [
                lambda: self.job_registry.close_all(reason="web application closed", timeout=5.0),
                self._finish_active_sessions,
                self.agent_thread_events.close,
                self.terminal_manager.close_all,
                self.runtime_event_stream.close,
                self.todo_store.close,
                self.settings.close,
            ]
        )
        errors: list[Exception] = []
        for action in actions:
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


__all__ = ["DEFAULT_DATA_ROOT", "WebAppState"]
