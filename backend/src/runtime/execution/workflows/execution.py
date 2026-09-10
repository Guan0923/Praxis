"""Default execution workflow."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities

from ...conversation.steering import consume_steering
from ...core.context import AgentRuntime
from ...core.contracts import WorkflowModeChanged
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from ..steps import ToolStepExecutor
from ..todo_finalization import (
    check_todo_finalization,
    refresh_todo_finalization_context,
)
from ..tool_batch import ToolBatchExecutor
from .budgets import _claim_model_turn, _ensure_tool_budget, _reject_over_budget_tools, _tool_batch_fits
from .common import (
    _apply_tool_batch_steering,
    _consume_agent_reports,
    _fail_pending_tools,
    _finish_assistant,
    _model_text_stream,
    _publish_assistant_message,
    _publish_repairs,
    _publish_tool_recovery,
    _start_assistant,
    _tool_failure_content,
)


class ExecutionWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()
        self._tool_batches = ToolBatchExecutor(self._steps)

    def run(self, runtime: AgentRuntime):
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support decisions.")
            return runtime.run
        while True:
            runtime.apply_pending_runtime_config()
            refresh_todo_finalization_context(runtime)
            if runtime.run.mode != "agent":
                raise WorkflowModeChanged("Agent workflow changed to Plan mode.")
            _consume_agent_reports(runtime)
            if cancel_if_requested(runtime):
                return runtime.run
            if consume_steering(runtime, phase="before_model_request") is not None:
                continue
            if not _ensure_tool_budget(runtime):
                return runtime.run
            if not _claim_model_turn(runtime, "decision"):
                return runtime.run
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                if cancel_if_requested(runtime):
                    return runtime.run
                interrupted = runtime.operation_interrupted()
                if consume_steering(runtime, phase="interrupted_model_response") is not None or interrupted:
                    continue
                fail_run(runtime, exc, **planning_failure_data(exc, capabilities.name))
                return runtime.run
            except BaseException:
                close()
                raise
            else:
                streamed = close()
            self._tool_batches.prepare(response)
            _publish_repairs(runtime, capabilities)
            todo_retry = not response.tool_messages and check_todo_finalization(
                runtime,
                response,
                content_streamed=streamed.content,
            )
            if not todo_retry:
                _publish_assistant_message(runtime, response, streamed)

            if cancel_if_requested(runtime):
                _fail_pending_tools(runtime, response, "Not executed because the run was cancelled.")
                return runtime.run

            if _consume_agent_reports(runtime):
                _fail_pending_tools(runtime, response, "Not executed because a subagent report arrived.")
                continue

            if consume_steering(runtime, phase="after_model_response") is not None:
                _fail_pending_tools(runtime, response, "Not executed because the user supplied new instructions.")
                continue

            if not response.tool_messages:
                if todo_retry:
                    continue
                complete_run(runtime, response, response_streamed=streamed.content)
                return runtime.run
            if not _tool_batch_fits(runtime, response):
                _reject_over_budget_tools(runtime, response)
                return runtime.run

            _start_assistant(runtime, response)
            try:
                batch = self._tool_batches.execute(runtime, response)
            except WorkflowModeChanged:
                _fail_pending_tools(runtime, response, "Not executed because the workflow mode changed.")
                _finish_assistant(runtime)
                raise
            for tool, outcome in zip(response.tool_messages, batch.outcomes, strict=True):
                if outcome.success or outcome.interrupt is not None:
                    continue
                error = outcome.error or "Tool execution failed without an error message."
                tool.content = _tool_failure_content(tool, error)
                _publish_tool_recovery(runtime, tool, error)
            if cancel_if_requested(runtime):
                return runtime.run
            if batch.steering is not None:
                _apply_tool_batch_steering(
                    runtime, batch.steering, next_tool_index=len(response.tool_messages), phase="after_tool_batch"
                )
                continue
            interrupt = next(
                (
                    outcome.interrupt
                    for outcome in batch.outcomes
                    if outcome.interrupt is not None and outcome.interrupt.choice != "deny"
                ),
                None,
            )
            if interrupt is not None:
                _finish_assistant(runtime)
                if interrupt.choice == "cancel":
                    cancel_run(runtime)
                else:
                    record_plan_feedback(runtime, interrupt.supplement)
                return runtime.run
            _finish_assistant(runtime)
