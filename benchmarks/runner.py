"""Execute one benchmark task against the real or rule-based agent."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import monotonic, perf_counter

from backend.providers import ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.conversation.trace import conversation_trace_records
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import persistent_event

from .containers import ContainerCancelled, ContainerTimeout, TaskContainer
from .event_collector import EventCollector
from .grading.programmatic import run_checkers
from .grading.scoring import aggregate_score
from .metrics import RunMetrics
from .model import BenchmarkTask, CheckContext, CheckerVerdict, TaskResult
from .sandbox import Sandbox
from .upstream import grade, prepare_environment


def auto_approve(request: InterruptRequest) -> InterruptDecision:
    """Approve every tool call and plan review so a headless run never stalls."""
    if request.kind == "plan":
        return InterruptDecision("implement")
    if request.kind == "tool":
        return InterruptDecision("continue")
    if request.kind == "question":
        answers = {question.id: [question.options[0].label] for question in request.questions}
        return InterruptDecision("answer", answers=answers)
    return InterruptDecision("continue")  # resume and anything unexpected


def build_metrics(collector: EventCollector, state, duration_ms: float) -> RunMetrics:
    finished = collector.run_finished or {}
    return RunMetrics(
        duration_ms=round(duration_ms, 3),
        model_calls=int(finished.get("model_calls", state.model_turns) or 0),
        tool_calls=int(finished.get("tool_calls", len(state.actions)) or 0),
        retries=int(finished.get("retries", 0) or 0),
        prompt_tokens=collector.prompt_tokens,
        completion_tokens=collector.completion_tokens,
        total_tokens=collector.total_tokens,
        active_skill_names=[skill.name for skill in state.active_skills],
        subagent_completed=collector.subagent_completed,
        subagent_failed=collector.subagent_failed,
    )


def apply_budget_overrides(task: BenchmarkTask, *, max_tool_calls: int | None) -> BenchmarkTask:
    if max_tool_calls is None:
        return task
    budgets = task.budgets
    return replace(
        task,
        budgets=replace(budgets, max_tool_calls=max_tool_calls or budgets.max_tool_calls),
    )


def _error_result(
    task: BenchmarkTask,
    message: str,
    *,
    attempt: int = 1,
    trace: list[dict] | None = None,
    failure_phase: str | None = None,
) -> TaskResult:
    diagnostic_event = RuntimeEvent(
        "error",
        message,
        {"failure_phase": failure_phase or "unknown"},
    )
    safe_message, _ = persistent_event(diagnostic_event, include_full_messages=True)
    return TaskResult(
        task_name=task.name,
        capability=task.capability,
        status="error",
        score=None,
        final_answer="",
        metrics=RunMetrics(0.0, 0, 0, 0, 0, 0, 0, []),
        verdicts=[],
        error=safe_message,
        passed=False,
        attempt=attempt,
        trace=trace if trace is not None else [],
        failure_phase=failure_phase,
    )


def run_one_task(
    task: BenchmarkTask,
    *,
    planner: str,
    sandbox: Sandbox,
    keep_workspaces: bool = False,
    max_tool_calls: int | None = None,
    attempt: int = 1,
    cancel_requested: Callable[[], bool] | None = None,
    on_phase: Callable[[str], None] | None = None,
    resource_cache: Path | None = None,
    on_event: Callable[[RuntimeEvent], None] | None = None,
) -> TaskResult:
    """Run one task end to end and return its graded result."""
    if planner not in task.planner_modes:
        return _error_result(
            task,
            f"planner {planner!r} is not supported by this task",
            attempt=attempt,
            failure_phase="configuration",
        )

    task = apply_budget_overrides(task, max_tool_calls=max_tool_calls)
    workspace: Path | None = None
    app = None
    conversation = None
    trace: list[dict] = []
    collector = EventCollector()
    phase = "workspace"
    container = None
    deadline: float | None = None
    timed_out = False

    def progress(value: str) -> None:
        nonlocal phase
        phase = value
        if on_phase is not None:
            on_phase(value)

    def collect(event: RuntimeEvent) -> None:
        collector(event)
        if on_event is not None:
            on_event(event)

    def cancelled() -> bool:
        nonlocal timed_out
        if deadline is not None and monotonic() >= deadline:
            timed_out = True
        return timed_out or (cancel_requested is not None and cancel_requested())

    try:
        progress("workspace")
        if cancelled():
            return replace(_error_result(task, "Run cancelled.", trace=trace), status="cancelled")
        workspace = sandbox.materialize_workspace(task)
        if task.container is not None:
            progress("environment")
            container = TaskContainer(task, cancelled, cache=resource_cache)
            prepare_environment(container)

        settings = RunnerSettings(
            max_tool_calls=task.budgets.max_tool_calls,
            log_full_messages=True,
        )
        progress("application")
        app = build_application(
            workspace,
            planner_name=planner,
            settings=settings,
            paths=sandbox.paths,
            model_config=sandbox.model_config,
            **(
                {
                    "tools_override": container.tools(),
                    "config_override": {
                        "memory": {"enabled": False},
                        "skills": {"enabled": False},
                        "mcp": {"enabled": False},
                        "subagents": {"enabled": False},
                    },
                }
                if container is not None
                else {}
            ),
        )
        conversation = app.open_conversation()
        started = perf_counter()
        progress("agent")
        deadline = monotonic() + task.budgets.timeout_seconds
        state = conversation.run_task(
            task.prompt,
            mode="agent",
            on_event=collect,
            interrupt=auto_approve,
            cancel_requested=cancelled,
        )
        duration_ms = (perf_counter() - started) * 1000.0

        metrics = build_metrics(collector, state, duration_ms)
        cancelled()
        deadline = None
        if timed_out:
            return replace(
                _error_result(task, "Task execution timed out.", trace=trace, failure_phase="timeout"),
                metrics=metrics,
            )
        context = CheckContext(
            task_name=task.name,
            workspace=workspace,
            status=state.status,
            final_answer=state.final_answer or "",
            metrics=metrics,
            tool_calls_by_name=dict(collector.tool_calls_by_name),
        )
        if cancelled():
            return replace(
                _error_result(task, "Run cancelled.", trace=trace, failure_phase="agent"),
                status="cancelled",
                metrics=metrics,
            )
        progress("grading")
        verdicts = grade(container) if container is not None else run_checkers(task, context)
        if state.status != "completed":
            verdicts = [CheckerVerdict(0.0, detail=f"agent run status: {state.status}")]
        score = aggregate_score(verdicts)
        return TaskResult(
            task_name=task.name,
            capability=task.capability,
            status=state.status,
            score=score,
            final_answer=state.final_answer or "",
            metrics=metrics,
            verdicts=verdicts,
            run_id=state.run_id,
            passed=score == 1.0,
            attempt=attempt,
            trace=trace,
            failure_phase="agent" if state.status != "completed" else None,
        )
    except ContainerCancelled:
        if timed_out:
            return _error_result(task, "Task execution timed out.", trace=trace, failure_phase="timeout")
        return replace(_error_result(task, "Run cancelled.", trace=trace, failure_phase=phase), status="cancelled")
    except ContainerTimeout:
        return _error_result(task, "Benchmark operation timed out.", trace=trace, failure_phase="timeout")
    except ModelConfigurationError as exc:
        return _error_result(
            task,
            f"model is not configured: {exc}",
            attempt=attempt,
            trace=trace,
            failure_phase="configuration",
        )
    except Exception as exc:  # keep the harness alive across task failures
        return _error_result(
            task,
            f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            trace=trace,
            failure_phase=phase,
        )
    finally:
        # Every result shares this list; collect finalized Items before closing
        # the application, including when execution or grading raised an error.
        finalization_errors: list[str] = []
        failure_phase = "cleanup"
        if conversation is not None and conversation.runtime is not None:
            try:
                runtime_state = conversation.runtime.state
                trace.extend(
                    conversation_trace_records(app.session_store, runtime_state.session_id, runtime_state.thread_id)
                )
            except Exception as exc:
                failure_phase = "trace"
                finalization_errors.append(f"Trace export failed: {type(exc).__name__}: {exc}")
        progress("cleanup")
        if container is not None:
            try:
                container.close()
            except Exception as exc:
                finalization_errors.append(f"{type(exc).__name__}: {exc}")
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                finalization_errors.append(f"{type(exc).__name__}: {exc}")
        if workspace is not None and not keep_workspaces:
            try:
                target = workspace.resolve()
                root = sandbox.workspaces_dir.resolve()
                if target == root or root not in target.parents:
                    raise ValueError("Benchmark cleanup path escapes its workspace root.")
                if target.exists():
                    shutil.rmtree(target)
            except Exception as exc:
                finalization_errors.append(f"{type(exc).__name__}: {exc}")
        if finalization_errors:
            return _error_result(
                task, "; ".join(finalization_errors), attempt=attempt, trace=trace, failure_phase=failure_phase
            )
