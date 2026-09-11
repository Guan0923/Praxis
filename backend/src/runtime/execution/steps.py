"""Tool-step execution policy driven entirely by AgentRuntime."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from threading import RLock
from time import perf_counter

from backend.domain import error_report, safe_error_message
from backend.sandbox import SandboxExecutionDecision
from backend.tools import ToolError, ToolInvocationContext

from ..core.context import AgentRuntime
from ..core.contracts import InterruptDecision, WorkflowModeChanged
from ..core.events import RuntimeEvent
from ..core.hooks import (
    HookOutcome,
    RunHookInfo,
    ToolHookContext,
    ToolHookResult,
    after_tool_hook_manager,
    before_tool_hook_manager,
)


class ToolQueueTimeout(ToolError):
    """A tool did not receive an execution slot before its deadline."""


USER_DENIED_FAILURE_CODE = "user_denied"
USER_DENIED_BATCH_FAILURE_CODE = "user_denied_batch"


@dataclass(frozen=True)
class ToolStepResult:
    success: bool
    output: str | None = None
    error: str | None = None
    interrupt: InterruptDecision | None = None
    retryable: bool | None = None


class ToolStepExecutor:
    """Execute runtime.state.active_message.tool_messages[active_tool_index]."""

    def execute(self, runtime: AgentRuntime) -> ToolStepResult:
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        if message is None or index is None or not 0 <= index < len(message.tool_messages):
            return self._failure(runtime, None, None, "unknown", "Runtime does not identify an active tool call.")
        runtime.apply_pending_runtime_config()
        return self.execute_call(runtime, message, index)

    def execute_call(
        self,
        runtime: AgentRuntime,
        message,
        index: int,
        *,
        approval_lock: RLock | None = None,
        execution_slot: Callable[[], AbstractContextManager[None]] | None = None,
        commit_lock: RLock | None = None,
        action_number: int | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> ToolStepResult:
        previous_mode = runtime.run.mode
        run = runtime.run
        if runtime.state.running_mode in {"agent", "plan"}:
            run.mode = runtime.state.running_mode  # type: ignore[assignment]
        if run.mode != previous_mode:
            raise WorkflowModeChanged(f"Workflow mode changed from {previous_mode} to {run.mode}.")
        tool_message = message.tool_messages[index]
        tool = tool_message.name
        tools = runtime.services.tools
        raw_publish = runtime.services.publish or (lambda _event: None)
        lock = commit_lock or RLock()

        def publish(event: RuntimeEvent) -> None:
            with lock:
                event_call_id = event.data.get("call_id")
                if event_call_id == tool_message.call_id and event.kind == "approval_requested":
                    tool_message.execution_stage = "waiting_approval"
                    runtime.save()
                raw_publish(event)

        try:
            if run.mode == "plan" and not tools.is_read_only(tool):
                return self._failure(
                    runtime, message, index, tool, f"Read-only Plan mode blocked tool: {tool}", commit_lock=lock
                )
            requires_confirmation = tools.requires_confirmation(tool)
            if tool == "write_stdin" and tool_message.arguments.get("chars", "") in ("", "\x03"):
                requires_confirmation = False
            read_only = tools.is_read_only(tool)
            workspace_confined = tools.is_workspace_confined(tool)
            retryable = tools.is_retryable(tool)
            validate = getattr(tools, "validate_arguments", None)
            if callable(validate):
                validate(tool, tool_message.arguments)
        except ToolError as exc:
            return self._failure(runtime, message, index, tool, exc, commit_lock=lock)

        context = ToolHookContext(
            run=RunHookInfo(runtime.state.session_id, run.run_id, run.task, run.mode),
            call_id=tool_message.call_id,
            name=tool,
            arguments=tool_message.arguments,
            workspace_root=runtime.state.workspace_root or "",
            project_cwd=runtime.state.project_cwd or "",
            permission_mode=runtime.state.permission_mode,
            requires_confirmation=requires_confirmation,
            read_only=read_only,
            workspace_confined=workspace_confined,
            sandbox_launcher=runtime.services.sandbox_launcher,
            sandbox_config=runtime.services.sandbox_config or {},
            sandbox_user_id=runtime.services.sandbox_user_id,
            interrupt=runtime.services.interrupt,
            record_event=lambda _kind, _message, _data: None,
            publish=publish,
        )
        with approval_lock if requires_confirmation and approval_lock is not None else nullcontext():
            if (cancel_requested or runtime.operation_interrupted)():
                return self._failure(
                    runtime,
                    message,
                    index,
                    tool,
                    "Tool was not started because this tool batch was interrupted.",
                    retryable=False,
                    failure_code="tool_batch_interrupted",
                    commit_lock=lock,
                )
            before = before_tool_hook_manager.execute(context, publish)
        if before.decision == "reject":
            interrupt = before.data.get("interrupt")
            decision = interrupt if isinstance(interrupt, InterruptDecision) else InterruptDecision("cancel")
            if decision.choice == "deny":
                return self._denied(runtime, message, index, tool, decision, commit_lock=lock)
            failure = self._failure(
                runtime,
                message,
                index,
                tool,
                before.reason or "Tool call rejected by hook.",
                retryable=False,
                commit_lock=lock,
            )
            return ToolStepResult(
                success=False,
                error=failure.error,
                interrupt=decision,
                retryable=False,
            )
        sandbox_data = before.data.get("sandbox_decision")
        sandbox_decision = sandbox_data if isinstance(sandbox_data, SandboxExecutionDecision) else None
        try:
            with execution_slot() if execution_slot is not None else nullcontext():
                result = self._invoke(
                    runtime,
                    message,
                    index,
                    retryable=retryable,
                    sandbox_decision=sandbox_decision,
                    commit_lock=lock,
                    action_number=action_number,
                    cancel_requested=cancel_requested or runtime.operation_interrupted,
                )
        except ToolError as exc:
            failure_code = "tool_queue_timeout" if isinstance(exc, ToolQueueTimeout) else "tool_batch_interrupted"
            result = self._failure(
                runtime,
                message,
                index,
                tool,
                exc,
                retryable=False,
                failure_code=failure_code,
                commit_lock=lock,
            )
        after_tool_hook_manager.execute(replace(context, outcome=self._hook_outcome(result)), publish)
        return result

    def _invoke(
        self,
        runtime: AgentRuntime,
        message,
        index: int,
        *,
        retryable: bool,
        sandbox_decision: SandboxExecutionDecision | None,
        commit_lock: RLock,
        action_number: int | None,
        cancel_requested: Callable[[], bool],
    ) -> ToolStepResult:
        run = runtime.run
        tool_message = message.tool_messages[index]
        tool = tool_message.name
        tools = runtime.services.tools
        publish = runtime.services.publish or (lambda _event: None)
        started_at = perf_counter()
        started_at_timestamp = runtime.services.clock()
        with commit_lock:
            tool_message.execution_stage = "running"
            publish(
                RuntimeEvent(
                    "tool_call",
                    tool,
                    self._event_data(
                        tool_message,
                        {
                            "arguments": tool_message.arguments,
                            "attempt": 1,
                            "started_at": started_at_timestamp,
                            "execution_stage": "running",
                        },
                    ),
                )
            )
            runtime.save()
        try:
            subagents = runtime.services.subagents
            if subagents is not None and subagents.handles(tool):
                result = subagents.invoke(runtime, tool, tool_message.arguments)
            else:
                invoke_with_context = getattr(tools, "invoke_with_context", None)
                if callable(invoke_with_context):
                    result = invoke_with_context(
                        tool,
                        tool_message.arguments,
                        ToolInvocationContext(
                            session_id=runtime.state.session_id,
                            turn_id=run.turn_id,
                            call_id=tool_message.call_id,
                            todo_store=runtime.services.todo_store,
                            timezone=runtime.state.timezone,
                            clock=runtime.services.clock,
                            job_scope=runtime.services.job_scope,
                            cancel_requested=cancel_requested,
                            register_abort=runtime.services.register_operation_abort,
                            sandbox_decision=sandbox_decision,
                        ),
                        confirmed=True,
                    )
                else:
                    result = tools.invoke(tool, tool_message.arguments, confirmed=True)
            if cancel_requested():
                raise ToolError("Tool invocation cancelled.")
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
            with commit_lock:
                tool_message.status = "succeeded"
                tool_message.content = result
                tool_message.retryable = retryable
                tool_message.execution_stage = "succeeded"
                run.completed_steps.append(action_number if action_number is not None else len(run.actions))
                publish(
                    RuntimeEvent(
                        "tool_result",
                        result,
                        self._event_data(
                            tool_message,
                            {
                                "tool": tool,
                                "duration_ms": duration_ms,
                                "attempts": 1,
                                "execution_stage": "succeeded",
                            },
                        ),
                    )
                )
                runtime.save()
            return ToolStepResult(success=True, output=result, retryable=retryable)
        except ToolError as exc:
            return self._failure(
                runtime,
                message,
                index,
                tool,
                exc,
                retryable=False if cancel_requested() else retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
                commit_lock=commit_lock,
            )
        except Exception as exc:
            # A tool failure is returned to the planner so it can select a
            # different action; it must not abort the surrounding run.
            return self._failure(
                runtime,
                message,
                index,
                tool,
                exc,
                retryable=retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
                commit_lock=commit_lock,
            )

    @staticmethod
    def _hook_outcome(result: ToolStepResult) -> HookOutcome[ToolHookResult]:
        if result.interrupt is not None and result.interrupt.choice != "deny":
            status = "cancelled"
        else:
            status = "succeeded" if result.success else "failed"
        return HookOutcome(
            status=status,
            result=ToolHookResult(
                result.success,
                result.output,
                result.error,
                result.retryable,
            ),
        )

    @staticmethod
    def _denied(
        runtime: AgentRuntime, message, index: int, tool: str, decision: InterruptDecision, *, commit_lock: RLock
    ) -> ToolStepResult:
        current = message.tool_messages[index]
        error = f"The user denied this {tool} tool call."
        with commit_lock:
            current.status = "failed"
            current.content = error
            current.retryable = False
            current.failure_code = USER_DENIED_FAILURE_CODE
            current.execution_stage = "failed"
            data = ToolStepExecutor._event_data(
                current,
                {
                    "tool": tool,
                    "error": error,
                    "failure_code": USER_DENIED_FAILURE_CODE,
                },
            )
            publish = runtime.services.publish or (lambda _event: None)
            publish(RuntimeEvent("tool_failed", error, data))
            runtime.save()
        return ToolStepResult(
            success=False,
            error=error,
            interrupt=decision,
            retryable=False,
        )

    @staticmethod
    def _failure(
        runtime: AgentRuntime,
        message,
        index: int | None,
        tool: str,
        error: str | BaseException,
        *,
        retryable: bool | None = None,
        duration_ms: float | None = None,
        failure_code: str | None = None,
        commit_lock: RLock | None = None,
    ) -> ToolStepResult:
        report = error_report(error) if isinstance(error, BaseException) else None
        error = (
            (getattr(error, "tool_output", None) or safe_error_message(error))
            if isinstance(error, BaseException)
            else error
        )
        call_id = ""
        lock = commit_lock or RLock()
        with lock:
            current = None
            if message is not None and index is not None and 0 <= index < len(message.tool_messages):
                current = message.tool_messages[index]
                call_id = current.call_id
                current.status = "failed"
                current.content = error
                current.retryable = retryable
                current.failure_code = failure_code
                current.execution_stage = "failed"
            data: dict[str, object] = {"call_id": call_id, "error": error}
            if report is not None:
                data["error_report"] = report
            if current is not None:
                data = ToolStepExecutor._event_data(current, data)
            if duration_ms is not None:
                data["duration_ms"] = duration_ms
            if failure_code is not None:
                data["failure_code"] = failure_code
            publish = runtime.services.publish or (lambda _event: None)
            publish(RuntimeEvent("tool_failed", error, {"tool": tool, **data}))
            runtime.save()
        return ToolStepResult(success=False, error=error, retryable=retryable)

    @staticmethod
    def _event_data(tool_message, values: dict[str, object]) -> dict[str, object]:
        return {
            "call_id": tool_message.call_id,
            "parallel_group_id": tool_message.parallel_group_id,
            "parallel_index": tool_message.parallel_index,
            "parallel_size": tool_message.parallel_size,
            **values,
        }
