from pathlib import Path

import pytest

from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import AssistantMessage, RunState, ToolMessage
from backend.domain.runtime_state import RuntimeState as Turn
from backend.planning.context_management import ContextCompactionResult
from backend.runtime import AgentApplication, AgentRunner
from backend.runtime.core.context import RuntimeState
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.storage.message_queue import MemoryMessageQueue
from backend.storage.sqlite import SQLiteSessionStore
from backend.tools import ToolRegistry


class LocalPlanner:
    name = "thread-runtime-test"

    def decide(self, runtime):
        if runtime.run.task == "interrupt":
            raise KeyboardInterrupt
        return AssistantMessage(content="done")


def conversations(tmp_path: Path):
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("thread isolation")
    app = AgentApplication(AgentRunner(LocalPlanner(), ToolRegistry(), checkpoints=store), store)
    main = app.open_conversation(session.session_id, thread_id=session.session_id)
    seed = main.run_task("shared history", mode="agent")
    forked = store.fork_turn_node(seed.turn_id, thread_id="branch", new_turn_id="forked")
    branch = app.open_conversation(session.session_id, thread_id=forked.thread_id)
    return app, store, main, branch


def test_initial_and_running_checkpoints_are_owned_by_thread(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("two threads")
    main = RuntimeState(session_id=session.session_id, status="running")
    main.current_run = RunState(task="main", mode="agent", run_id="run-main", thread_id=main.thread_id)
    store.save_runtime(main)
    branch = RuntimeState(session_id=session.session_id, thread_id="branch")
    store.save_runtime(branch)
    assert store.load_runtime(session.session_id).to_dict() == main.to_dict()
    assert store.load_runtime(session.session_id, thread_id="branch").to_dict() == branch.to_dict()
    store.start_turn(session.session_id, "run-branch", "branch", thread_id="branch")
    assert store.running_run_id(session.session_id, thread_id=main.thread_id) == "run-main"
    assert store.running_run_id(session.session_id, thread_id="branch") == "run-branch"


def test_new_branch_uses_its_own_history_and_configuration(tmp_path: Path) -> None:
    app, store, main, branch = conversations(tmp_path)
    main.run_task("main only", mode="agent")
    main_before = store.load_runtime(main.active_session.session_id).to_dict()
    seen = []

    def capture(event):
        if event.kind == "run_started":
            seen.extend(message.content for message in branch.runtime.model_messages())

    completed = branch.run_task("branch only", mode="agent", on_event=capture)
    assert completed.thread_id == "branch"
    assert "shared history" in seen and "branch only" in seen
    assert "main only" not in seen
    assert store.load_runtime(main.active_session.session_id).to_dict() == main_before
    reopened = app.open_conversation(main.active_session.session_id, thread_id="branch")
    assert reopened.runtime.run.run_id == completed.run_id
    assert reopened.runtime.state.thread_id == "branch"


def test_resume_is_bound_to_requested_thread_and_turn(tmp_path: Path) -> None:
    app, store, main, branch = conversations(tmp_path)
    main_paused = main.run_task("main pending", mode="agent", suspend_requested=lambda: True)
    branch_paused = branch.run_task("branch pending", mode="agent", suspend_requested=lambda: True)
    session_id = main.active_session.session_id
    main_before = store.load_runtime(session_id).to_dict()
    reopened = app.open_conversation(session_id, thread_id="branch")
    assert reopened.prepare_resume(turn_id=branch_paused.turn_id).run_id == branch_paused.run_id
    with pytest.raises(ValueError, match="does not match"):
        reopened.resume_session(turn_id=main_paused.turn_id, resume_confirmed=True)
    assert store.find_node(branch_paused.turn_id).status == "paused"
    resumed = reopened.resume_session(turn_id=branch_paused.turn_id, resume_confirmed=True)
    assert resumed.status == "completed" and resumed.turn_id == branch_paused.turn_id
    assert store.load_runtime(session_id).to_dict() == main_before
    assert store.find_node(main_paused.turn_id).status == "paused"


def test_startup_finishes_each_interrupted_thread_independently(tmp_path: Path) -> None:
    _app, store, main, branch = conversations(tmp_path)
    for conversation in (main, branch):
        with pytest.raises(KeyboardInterrupt):
            conversation.run_task("interrupt", mode="agent")
    session_id = main.active_session.session_id
    attempts = [conversation.runtime.run for conversation in (main, branch)]
    reopened = WebAppState(tmp_path / "data", message_queue=MemoryMessageQueue())
    try:
        for attempt in attempts:
            saved = store.load_runtime(session_id, thread_id=attempt.thread_id)
            assert saved.status == "idle" and saved.current_run.status == "failed"
            assert saved.current_run.run_id == attempt.run_id
            assert store.find_node(attempt.turn_id).status == "failed"
            assert store.running_run_id(session_id, thread_id=attempt.thread_id) is None
    finally:
        reopened.close()


def test_resume_does_not_switch_attempts_during_confirmation(tmp_path: Path) -> None:
    app, store, main, branch = conversations(tmp_path)
    paused = branch.run_task("branch pending", mode="agent", suspend_requested=lambda: True)
    session_id = main.active_session.session_id
    reopened = app.open_conversation(session_id, thread_id="branch")

    def confirm(_request):
        branch.run_task("new branch task", mode="agent")
        return InterruptDecision("continue")

    with pytest.raises(ValueError, match="changed after resume"):
        reopened.resume_session(turn_id=paused.turn_id, interrupt=confirm)
    assert store.find_node(paused.turn_id).status == "paused"
    assert store.load_runtime(session_id, thread_id="branch").current_run.task == "new branch task"


def test_branch_plan_handoff_and_compaction_keep_thread(tmp_path: Path) -> None:
    class PlanPlanner(LocalPlanner):
        def decide(self, runtime):
            if runtime.run.mode == "plan":
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name=REQUEST_PLAN_REVIEW_NAME,
                            call_id="review-branch",
                            arguments={"plan": "branch work"},
                        )
                    ]
                )
            return super().decide(runtime)

        def compact_context(self, runtime):
            runtime.services.publish(
                RuntimeEvent(
                    "context_compaction_completed",
                    "compacted",
                    {"summary": "branch summary"},
                )
            )
            return ContextCompactionResult(True, 4, 1, "branch summary")

    _app, store, main, branch = conversations(tmp_path)
    main.run_task("main pending", mode="agent", suspend_requested=lambda: True)
    session_id = main.active_session.session_id
    main_before = store.load_runtime(session_id).to_dict()
    branch.runner = AgentRunner(PlanPlanner(), ToolRegistry(), checkpoints=store)
    result = branch.run_task("plan branch", mode="plan", interrupt=lambda request: InterruptDecision("implement"))
    assert result.status == "completed" and result.thread_id == "branch"
    turns = [node for node in store.load_nodes(session_id) if isinstance(node, Turn) and node.thread_id == "branch"]
    assert len(turns) == 3
    assert turns[-1].parent_id == turns[-2].id
    compacted = branch.compact_turn(result.turn_id, "branch-compact")
    assert compacted.status == "success" and compacted.thread_id == "branch"
    assert store.load_runtime(session_id).to_dict() == main_before
