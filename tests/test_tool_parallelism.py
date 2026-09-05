"""Real threaded coverage for one assistant tool batch."""

from __future__ import annotations

import threading
import time

import pytest

from backend.domain import AssistantMessage, ToolMessage
from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner, RunnerSettings
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.execution.tool_batch import ToolBatchExecutor
from backend.tools import Tool, ToolRegistry


class ConcurrencyProbe:
    def __init__(self, expected: int | None = None) -> None:
        self.expected = expected
        self.active = 0
        self.maximum = 0
        self.entered = 0
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.release = threading.Event()

    def run(self, **_arguments) -> str:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.maximum = max(self.maximum, self.active)
            if self.expected is not None and self.entered >= self.expected:
                self.ready.set()
        self.release.wait(5.0)
        with self.lock:
            self.active -= 1
        return "ok"


def runtime_for(tools: ToolRegistry, *, parallel: int):
    runner = AgentRunner(RuleBasedPlanner(), tools, max_tool_parellel=parallel)
    return runner.new_runtime(task="parallel tools")


def execute_in_thread(runtime, message: AssistantMessage):
    runtime.state.active_message = message
    result = []
    worker = threading.Thread(target=lambda: result.append(ToolBatchExecutor().execute(runtime, message)), daemon=True)
    worker.start()
    return worker, result


def test_default_parallel_limit_runs_sixteen_and_queues_seventeenth() -> None:
    probe = ConcurrencyProbe(expected=16)
    tools = ToolRegistry([Tool("inspect", "inspect", probe.run)])
    runtime = runtime_for(tools, parallel=16)
    message = AssistantMessage(
        tool_messages=[ToolMessage(name="inspect", call_id=f"call_{index}") for index in range(17)]
    )

    worker, result = execute_in_thread(runtime, message)
    assert probe.ready.wait(3.0)
    time.sleep(0.1)
    assert probe.maximum == 16
    assert probe.entered == 16
    probe.release.set()
    worker.join(5.0)

    assert not worker.is_alive()
    assert len(result) == 1
    assert all(outcome.success for outcome in result[0].outcomes)


def test_parallel_setting_one_is_serial_and_values_above_sixteen_apply() -> None:
    serial = ConcurrencyProbe()
    serial.release.set()
    serial_runtime = runtime_for(ToolRegistry([Tool("inspect", "inspect", serial.run)]), parallel=1)
    serial_message = AssistantMessage(
        tool_messages=[ToolMessage(name="inspect", call_id=f"serial_{index}") for index in range(5)]
    )
    serial_runtime.state.active_message = serial_message
    ToolBatchExecutor().execute(serial_runtime, serial_message)
    assert serial.maximum == 1

    parallel = ConcurrencyProbe(expected=20)
    parallel_runtime = runtime_for(ToolRegistry([Tool("inspect", "inspect", parallel.run)]), parallel=20)
    parallel_message = AssistantMessage(
        tool_messages=[ToolMessage(name="inspect", call_id=f"parallel_{index}") for index in range(20)]
    )
    worker, _result = execute_in_thread(parallel_runtime, parallel_message)
    assert parallel.ready.wait(3.0)
    assert parallel.maximum == 20
    parallel.release.set()
    worker.join(5.0)
    assert not worker.is_alive()


def test_serial_subqueue_stays_single_while_normal_tools_run() -> None:
    serial = ConcurrencyProbe()
    normal = ConcurrencyProbe(expected=2)
    tools = ToolRegistry(
        [
            Tool("todo_write", "todo", serial.run),
            Tool("inspect", "inspect", normal.run),
        ]
    )
    runtime = runtime_for(tools, parallel=4)
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="todo_write", call_id="todo_1"),
            ToolMessage(name="todo_write", call_id="todo_2"),
            ToolMessage(name="inspect", call_id="inspect_1"),
            ToolMessage(name="inspect", call_id="inspect_2"),
        ]
    )

    worker, _result = execute_in_thread(runtime, message)
    assert normal.ready.wait(3.0)
    time.sleep(0.1)
    assert serial.maximum == 1
    assert normal.maximum == 2
    serial.release.set()
    normal.release.set()
    worker.join(5.0)
    assert not worker.is_alive()


def test_same_file_writes_are_serialized_and_results_keep_model_order() -> None:
    probe = ConcurrencyProbe()
    probe.release.set()

    def write_file(path: str, content: str) -> str:
        del path
        time.sleep(0.05 if content == "first" else 0.01)
        return probe.run(content=content) + ":" + content

    tools = ToolRegistry([Tool("write_file", "write", write_file, read_only=False)])
    runtime = runtime_for(tools, parallel=2)
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="write_file", call_id="write_1", arguments={"path": "same.txt", "content": "first"}),
            ToolMessage(name="write_file", call_id="write_2", arguments={"path": "same.txt", "content": "second"}),
        ]
    )
    runtime.state.active_message = message

    result = ToolBatchExecutor().execute(runtime, message)

    assert probe.maximum == 1
    assert [outcome.output for outcome in result.outcomes] == ["ok:first", "ok:second"]
    assert [tool.parallel_index for tool in message.tool_messages] == [0, 1]
    assert len({tool.parallel_group_id for tool in message.tool_messages}) == 1


def test_completion_events_are_real_time_but_results_keep_model_order() -> None:
    def inspect(label: str, delay: float) -> str:
        time.sleep(delay)
        return label

    tools = ToolRegistry([Tool("inspect", "inspect", inspect)])
    runtime = runtime_for(tools, parallel=2)
    events = []
    runtime.services.publish = events.append
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="inspect", call_id="slow", arguments={"label": "first", "delay": 0.08}),
            ToolMessage(name="inspect", call_id="fast", arguments={"label": "second", "delay": 0.01}),
        ]
    )
    runtime.state.active_message = message

    result = ToolBatchExecutor().execute(runtime, message)

    assert [outcome.output for outcome in result.outcomes] == ["first", "second"]
    assert [event.data["call_id"] for event in events if event.kind == "tool_result"] == ["fast", "slow"]


def test_runner_settings_defaults_and_validation() -> None:
    settings = RunnerSettings()
    assert settings.max_tool_calls == 512
    assert settings.max_tool_parellel == 16
    assert RunnerSettings(max_tool_parellel=64).max_tool_parellel == 64
    with pytest.raises(ValueError, match="positive integer"):
        RunnerSettings(max_tool_parellel=0)


def test_queue_timeout_fails_only_the_waiting_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.runtime.execution import tool_batch

    monkeypatch.setattr(tool_batch, "_QUEUE_TIMEOUT_SECONDS", 0.1)
    probe = ConcurrencyProbe(expected=1)
    tools = ToolRegistry([Tool("inspect", "inspect", probe.run)])
    runtime = runtime_for(tools, parallel=1)
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="inspect", call_id="timeout_1"),
            ToolMessage(name="inspect", call_id="timeout_2"),
        ]
    )

    worker, result = execute_in_thread(runtime, message)
    assert probe.ready.wait(3.0)
    time.sleep(0.2)
    probe.release.set()
    worker.join(5.0)

    assert not worker.is_alive()
    assert probe.entered == 1
    assert sorted(outcome.success for outcome in result[0].outcomes) == [False, True]
    failed = next(tool for tool in message.tool_messages if tool.status == "failed")
    assert failed.failure_code == "tool_queue_timeout"
    assert "timed out after 90 seconds" in (failed.content or "")


def test_approval_is_serial_but_does_not_block_normal_tools_and_denial_is_isolated() -> None:
    normal_completed = threading.Event()
    approval_active = 0
    maximum_approvals = 0
    approval_lock = threading.Lock()
    executed: list[str] = []

    def protected(value: str) -> str:
        executed.append(value)
        return value

    def normal() -> str:
        executed.append("normal")
        normal_completed.set()
        return "normal"

    def interrupt(request) -> InterruptDecision:
        nonlocal approval_active, maximum_approvals
        with approval_lock:
            approval_active += 1
            maximum_approvals = max(maximum_approvals, approval_active)
        assert normal_completed.wait(3.0)
        time.sleep(0.02)
        with approval_lock:
            approval_active -= 1
        return InterruptDecision("deny" if request.data["call_id"] == "denied" else "continue")

    tools = ToolRegistry(
        [
            Tool("protected", "protected", protected, requires_confirmation=True),
            Tool("inspect", "inspect", normal),
        ]
    )
    runtime = runtime_for(tools, parallel=3)
    runtime.services.interrupt = interrupt
    runtime.state.permission_mode = "read_only"
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="protected", call_id="denied", arguments={"value": "denied"}),
            ToolMessage(name="inspect", call_id="normal"),
            ToolMessage(name="protected", call_id="approved", arguments={"value": "approved"}),
        ]
    )
    runtime.state.active_message = message

    result = ToolBatchExecutor().execute(runtime, message)

    assert maximum_approvals == 1
    assert executed == ["normal", "approved"]
    assert [outcome.success for outcome in result.outcomes] == [False, True, True]
    assert message.tool_messages[0].failure_code == "user_denied"
    assert message.tool_messages[2].status == "succeeded"


def test_batch_interrupt_cancels_running_tool_and_clears_queued_tools() -> None:
    probe = ConcurrencyProbe(expected=1)
    cancelled = threading.Event()
    tools = ToolRegistry([Tool("inspect", "inspect", probe.run)])
    runtime = runtime_for(tools, parallel=1)
    runtime.services.cancel_requested = cancelled.is_set
    message = AssistantMessage(
        tool_messages=[
            ToolMessage(name="inspect", call_id="running"),
            ToolMessage(name="inspect", call_id="queued"),
        ]
    )

    worker, result = execute_in_thread(runtime, message)
    assert probe.ready.wait(3.0)
    cancelled.set()
    time.sleep(0.1)
    probe.release.set()
    worker.join(5.0)

    assert not worker.is_alive()
    assert [outcome.success for outcome in result[0].outcomes] == [False, False]
    assert message.tool_messages[0].content == "Tool invocation cancelled."
    assert message.tool_messages[1].failure_code == "tool_batch_interrupted"
    assert "batch was interrupted" in (message.tool_messages[1].content or "")
