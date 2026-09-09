"""Interactive Plan-mode proposal workflow."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities

from ...conversation.steering import consume_steering
from ...conversation.user_input import REQUEST_USER_INPUT_NAME
from ...core.context import AgentRuntime
from ...core.contracts import WorkflowModeChanged
from ...planning.review import REQUEST_PLAN_REVIEW_NAME
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, fail_run, planning_failure_data, record_plan_feedback
from ..steps import ToolStepExecutor, ToolStepResult
from ..tool_batch import ToolBatchExecutor
from .budgets import _claim_model_turn, _ensure_tool_budget, _reject_over_budget_tools, _tool_batch_fits
from .common import (
    PlanProposalResult,
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
from .controls import PlanControlMixin


class PlanProposalWorkflow(PlanControlMixin):
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()
        self._submitted_plan: str | None = None
        self._tool_batches = ToolBatchExecutor(
            self._steps,
            self._execute_special_tool,
            frozenset({REQUEST_USER_INPUT_NAME, REQUEST_PLAN_REVIEW_NAME}),
        )

    def _execute_special_tool(self, runtime, message, index, commit_lock) -> ToolStepResult | None:
        tool = message.tool_messages[index]
        if tool.name == REQUEST_USER_INPUT_NAME:
            return self._execute_user_input_call(runtime, tool, commit_lock)
        if tool.name == REQUEST_PLAN_REVIEW_NAME:
            outcome, plan = self._execute_plan_review_call(runtime, tool, commit_lock)
            if plan is not None:
                with commit_lock:
                    self._submitted_plan = plan
            return outcome
        return None

    def prepare(self, runtime: AgentRuntime) -> PlanProposalResult | None:
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support plan proposals.")
            return None
        while True:
            runtime.apply_pending_runtime_config()
            if runtime.run.mode != "plan":
                raise WorkflowModeChanged("Plan workflow changed to Agent mode.")
            _consume_agent_reports(runtime)
            if cancel_if_requested(runtime):
                return None
            if consume_steering(runtime, phase="before_model_request") is not None:
                continue
            if not _ensure_tool_budget(runtime):
                return None
            if not _claim_model_turn(runtime, "decision"):
                return None
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                if cancel_if_requested(runtime):
                    return None
                interrupted = runtime.operation_interrupted()
                if consume_steering(runtime, phase="interrupted_model_response") is not None or interrupted:
                    continue
                fail_run(runtime, exc, **planning_failure_data(exc, capabilities.name))
                return None
            except BaseException:
                close()
                raise
            else:
                streamed = close()
            self._tool_batches.prepare(response)
            _publish_repairs(runtime, capabilities)
            _publish_assistant_message(runtime, response, streamed)
            if cancel_if_requested(runtime):
                _fail_pending_tools(runtime, response, "Not executed because the run was cancelled.")
                return None
            if _consume_agent_reports(runtime):
                _fail_pending_tools(runtime, response, "Not executed because a subagent report arrived.")
                continue
            if consume_steering(runtime, phase="after_model_response") is not None:
                _fail_pending_tools(runtime, response, "Not executed because the user supplied new instructions.")
                continue
            if not response.tool_messages:
                _start_assistant(runtime, response)
                _finish_assistant(runtime)
                return PlanProposalResult(response, content_streamed=streamed.content)
            if not _tool_batch_fits(runtime, response):
                _reject_over_budget_tools(runtime, response)
                return None
            _start_assistant(runtime, response)
            self._submitted_plan = None
            try:
                batch = self._tool_batches.execute(runtime, response)
            except WorkflowModeChanged:
                _fail_pending_tools(runtime, response, "Not executed because the workflow mode changed.")
                _finish_assistant(runtime)
                raise
            for tool, outcome in zip(response.tool_messages, batch.outcomes, strict=True):
                if outcome.success or outcome.interrupt is not None:
                    continue
                error = outcome.error or "Tool failed."
                tool.content = _tool_failure_content(tool, error)
                _publish_tool_recovery(runtime, tool, error)
            if cancel_if_requested(runtime):
                return None
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
                return None
            _finish_assistant(runtime)
            if self._submitted_plan is not None:
                return PlanProposalResult(response, self._submitted_plan, streamed.content)
