"""Bounded parallel execution for one assistant tool-call batch."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Event, RLock
from time import monotonic

from backend.domain import AssistantMessage
from backend.tools import ToolError

from ..conversation.steering import SteeringUpdate, collect_steering
from ..core.context import AgentRuntime
from ..core.contracts import WorkflowModeChanged
from ..core.events import RuntimeEvent
from .steps import ToolQueueTimeout, ToolStepExecutor, ToolStepResult

_QUEUE_TIMEOUT_SECONDS = 90.0
_SERIAL_TOOLS = frozenset(
    {
        "request_user_input",
        "request_plan_review",
        "todo_write",
        "delegate_tasks",
        "send_agent_message",
        "set_thread_node_status",
        "get_thread_node",
        "pause_current_turn",
        "create_directory",
    }
)


@dataclass(frozen=True)
class ToolBatchResult:
    outcomes: tuple[ToolStepResult, ...]
    steering: SteeringUpdate | None = None


class _FairGate:
    """FIFO capacity gate with bounded, cancellation-aware waiting."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._condition = Condition()
        self._waiting: deque[object] = deque()
        self._active = 0

    @contextmanager
    def enter(self, deadline: float, stop: Event) -> Iterator[None]:
        token = object()
        acquired = False
        with self._condition:
            self._waiting.append(token)
            while True:
                remaining = deadline - monotonic()
                if stop.is_set():
                    self._waiting.remove(token)
                    self._condition.notify_all()
                    raise ToolError("Tool was not started because this tool batch was interrupted.")
                if self._waiting[0] is token and self._active < self._capacity:
                    self._waiting.popleft()
                    self._active += 1
                    acquired = True
                    self._condition.notify_all()
                    break
                if remaining <= 0:
                    self._waiting.remove(token)
                    self._condition.notify_all()
                    raise ToolQueueTimeout("Tool execution queue timed out after 90 seconds.")
                self._condition.wait(timeout=min(remaining, 0.05))
        try:
            yield
        finally:
            if acquired:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()


class ToolBatchExecutor:
    """Execute one model response while preserving model-order results."""

    def __init__(
        self,
        steps: ToolStepExecutor | None = None,
        special_executor: Callable[[AgentRuntime, AssistantMessage, int, RLock], ToolStepResult | None] | None = None,
        special_tool_names: frozenset[str] = frozenset(),
    ) -> None:
        self._steps = steps or ToolStepExecutor()
        self._special_executor = special_executor
        self._special_tool_names = special_tool_names

    @staticmethod
    def prepare(message: AssistantMessage) -> None:
        """Assign stable batch metadata before the assistant message is published."""

        tools = message.tool_messages
        group_id = f"parallel:{tools[0].call_id}" if len(tools) > 1 else None
        for index, tool in enumerate(tools):
            tool.parallel_group_id = group_id
            tool.parallel_index = index if group_id else None
            tool.parallel_size = len(tools) if group_id else None
            tool.execution_stage = "checking"

    def execute(self, runtime: AgentRuntime, message: AssistantMessage) -> ToolBatchResult:
        tools = message.tool_messages
        if not tools:
            return ToolBatchResult(())
        previous_mode = runtime.run.mode
        runtime.apply_pending_runtime_config()
        if runtime.run.mode != previous_mode:
            raise WorkflowModeChanged(f"Workflow mode changed from {previous_mode} to {runtime.run.mode}.")
        self.prepare(message)
        parallel = min(len(tools), runtime.state.runner_settings.max_tool_parellel)
        commit_lock = RLock()
        approval_lock = RLock()
        serial_gate = _FairGate(1)
        slots = _FairGate(parallel)
        stop = Event()
        steering_lock = RLock()
        path_gates: dict[str, _FairGate] = {}
        outcomes: list[ToolStepResult | None] = [None] * len(tools)
        steering: SteeringUpdate | None = None

        with commit_lock:
            action_offset = len(runtime.run.actions)
            runtime.state.active_tool_index = None
            for index, tool in enumerate(tools):
                runtime.run.actions.append(tool)
            runtime.save()

        def resource_gate(index: int) -> _FairGate | None:
            tool = tools[index]
            if tool.name not in {"write_file", "edit_file"}:
                return None
            path = tool.arguments.get("path")
            if not isinstance(path, str):
                return None
            with commit_lock:
                return path_gates.setdefault(path.casefold(), _FairGate(1))

        def ensure_start_allowed() -> None:
            nonlocal steering
            if runtime.operation_interrupted():
                stop.set()
            with steering_lock:
                if steering is None:
                    steering = collect_steering(runtime)
                    if steering is not None:
                        stop.set()
            if stop.is_set():
                raise ToolError("Tool was not started because this tool batch was interrupted.")

        @contextmanager
        def execution_slot(index: int) -> Iterator[None]:
            deadline = monotonic() + _QUEUE_TIMEOUT_SECONDS
            with commit_lock:
                tool = tools[index]
                tool.execution_stage = "queued"
                publish = runtime.services.publish or (lambda _event: None)
                publish(
                    RuntimeEvent(
                        "tool_queued",
                        tool.name,
                        {
                            "call_id": tool.call_id,
                            "parallel_group_id": tool.parallel_group_id,
                            "parallel_index": tool.parallel_index,
                            "parallel_size": tool.parallel_size,
                            "execution_stage": "queued",
                        },
                    )
                )
                runtime.save()
            if stop.is_set():
                raise ToolError("Tool was not started because this tool batch was interrupted.")
            with slots.enter(deadline, stop):
                ensure_start_allowed()
                serial = serial_gate if tools[index].name in _SERIAL_TOOLS else None
                resource = resource_gate(index)
                contexts = [gate.enter(deadline, stop) for gate in (serial, resource) if gate is not None]
                if not contexts:
                    try:
                        yield
                    finally:
                        if tools[index].name == "pause_current_turn" and runtime.services.pause_after_tool:
                            stop.set()
                    return
                with contexts[0]:
                    ensure_start_allowed()
                    if len(contexts) == 1:
                        try:
                            yield
                        finally:
                            if tools[index].name == "pause_current_turn" and runtime.services.pause_after_tool:
                                stop.set()
                    else:
                        with contexts[1]:
                            ensure_start_allowed()
                            try:
                                yield
                            finally:
                                if tools[index].name == "pause_current_turn" and runtime.services.pause_after_tool:
                                    stop.set()

        def run_one(index: int) -> ToolStepResult:
            if self._special_executor is not None and tools[index].name in self._special_tool_names:
                with execution_slot(index):
                    special = self._special_executor(runtime, message, index, commit_lock)
                if special is None:
                    raise RuntimeError(f"Special tool executor did not handle {tools[index].name}.")
                return special
            return self._steps.execute_call(
                runtime,
                message,
                index,
                approval_lock=approval_lock,
                execution_slot=lambda: execution_slot(index),
                commit_lock=commit_lock,
                action_number=action_offset + index + 1,
                cancel_requested=lambda: stop.is_set() or runtime.operation_interrupted(),
            )

        with steering_lock:
            steering = collect_steering(runtime)
            if steering is not None:
                stop.set()
        fatal: BaseException | None = None
        mode_change_requested = False
        with ThreadPoolExecutor(max_workers=len(tools), thread_name_prefix="praxis-tool") as pool:
            futures: dict[Future[ToolStepResult], int] = {
                pool.submit(run_one, index): index for index in range(len(tools))
            }
            pending = set(futures)
            while pending:
                completed, pending = wait(pending, timeout=0.05)
                for future in completed:
                    index = futures[future]
                    try:
                        outcome = future.result()
                    except BaseException as exc:
                        fatal = fatal or exc
                        stop.set()
                        continue
                    outcomes[index] = outcome
                    if outcome.interrupt is not None and outcome.interrupt.choice != "deny":
                        stop.set()
                if steering is None:
                    with steering_lock:
                        if steering is None:
                            steering = collect_steering(runtime)
                            if steering is not None:
                                stop.set()
                if runtime.operation_interrupted():
                    stop.set()

                pending_config = runtime.services.pending_runtime_config
                target_mode = pending_config.running_mode if pending_config is not None else None
                if target_mode in {"agent", "plan"} and target_mode != runtime.run.mode:
                    mode_change_requested = True
                    stop.set()

        if fatal is not None:
            raise fatal
        if mode_change_requested:
            previous_mode = runtime.run.mode
            runtime.apply_pending_runtime_config()
            raise WorkflowModeChanged(f"Workflow mode changed from {previous_mode} to {runtime.run.mode}.")

        normalized = tuple(
            outcome or ToolStepResult(False, error="Tool execution ended without a result.", retryable=False)
            for outcome in outcomes
        )
        return ToolBatchResult(normalized, steering)


__all__ = ["ToolBatchExecutor", "ToolBatchResult"]
