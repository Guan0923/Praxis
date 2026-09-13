from __future__ import annotations

import base64
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from backend.api.chat import interrupts
from backend.domain import AssistantMessage, ToolMessage
from backend.jobs import CommandError
from backend.runtime import AgentRunner
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.sandbox import FileAccessMode, NetworkMode, ResourceLimits, SandboxExecutionDecision
from backend.tools import ToolInvocationContext, ToolRegistry, WorkspaceCommand
from backend.tools.default_tools.command import command_tool
from tests.testing_sandbox import DirectTestSandboxLauncher, FakeCommandAudit


class DenyingLauncher(DirectTestSandboxLauncher):
    def __init__(self, *, diagnostic: str = "", delay: float = 0, audited: bool = True) -> None:
        super().__init__()
        self.diagnostic = diagnostic
        self.delay = delay
        self.audited = audited

    def command_audit(self, _process):
        return FakeCommandAudit(({"event_id": 4656, "record_id": 123, "process_id": 100},) if self.audited else ())

    def popen_factory(self, policy, **options):
        factory = super().popen_factory(policy, **options)

        def launch(_argv, **kwargs):
            script = (
                "import sys,time; "
                f"sys.stderr.write({self.diagnostic!r}); sys.stderr.flush(); "
                f"time.sleep({self.delay}); sys.exit(1)"
            )
            return factory([sys.executable, "-c", script], **kwargs)

        return launch


def command_context(tmp_path, launcher, approve):
    return ToolInvocationContext(
        session_id="session-escalation",
        turn_id="turn-escalation",
        sandbox_decision=SandboxExecutionDecision(
            launcher=launcher,
            workspaces=(tmp_path,),
            session_id="session-escalation",
            user_id="local",
            file_mode=FileAccessMode.WORKSPACE_WRITE,
            network_mode=NetworkMode.NO_NETWORK,
            network_allowlist=(),
            proxy_port=17831,
            limits=ResourceLimits(),
        ),
        approve_command_escalation=approve,
    )


@pytest.mark.parametrize("choice", ["continue", "deny"])
@pytest.mark.parametrize("retry_fails", [False, True])
def test_one_tool_call_emits_only_the_selected_attempt_result(tmp_path: Path, choice: str, retry_fails: bool) -> None:
    commands = WorkspaceCommand(tmp_path)
    launcher = DenyingLauncher()
    events = []
    requests = []
    command = "echo SECOND-ATTEMPT" if not retry_fails else "echo SECOND-ATTEMPT 1>&2 & exit /b 7"
    if os.name != "nt":
        command = "printf SECOND-ATTEMPT" if not retry_fails else "echo SECOND-ATTEMPT >&2; exit 7"

    class Planner:
        calls = 0

        def decide(self, _runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(name="run_command", call_id="call-escalation", arguments={"cmd": command})
                    ]
                )
            return AssistantMessage(content="done")

    def approve(request):
        requests.append(request)
        assert not [event for event in events if event.kind in {"tool_result", "tool_failed"}]
        assert not launcher._leases
        return InterruptDecision(choice)

    runner = AgentRunner(
        Planner(),
        ToolRegistry([command_tool(commands)]),
        workspace_root=str(tmp_path),
        sandbox_launcher=launcher,
        sandbox_config={},
    )
    runtime = runner.new_runtime(task="test escalation", interrupt=approve, on_event=events.append)
    runtime.services.runtime_node_event = events.append
    runtime.state.permission_mode = "workspace_write"
    try:
        result = runner.run(runtime)
        tool = next(
            item
            for message in result.history
            if isinstance(message, AssistantMessage)
            for item in message.tool_messages
        )
        final_events = [event for event in events if event.kind in {"tool_result", "tool_failed"}]
        assert len(final_events) == len(requests) == len(launcher.policies) == 1, (
            [event.kind for event in events],
            tool.content,
        )
        assert final_events[0].data["call_id"] == "call-escalation"
        if choice == "deny":
            assert tool.content == requests[0].data["details"]
            assert tool.failure_code == "user_denied"
        else:
            payload = json.loads(tool.content)
            assert payload["exit_code"] == (7 if retry_fails else 0)
            assert payload["escalated"] is True
            assert "SECOND-ATTEMPT" in payload["output"]
            assert "Access is denied" not in payload["output"]
        assert runtime.state.permission_mode == "workspace_write"
        assert runtime.run.tool_calls == 1
    finally:
        commands.close()


def test_polling_remembers_denial_and_uses_the_current_approval_callback(tmp_path: Path) -> None:
    launcher = DenyingLauncher(delay=0.3)
    approvals = []
    context = command_context(tmp_path, launcher, None)
    commands = WorkspaceCommand(tmp_path)
    try:
        first = json.loads(commands.run_with_context(context, "echo retry-from-poll", yield_time_ms=0))
        current = replace(context, approve_command_escalation=lambda *args: approvals.append(args) or True)
        result = json.loads(commands.write_with_context(current, first["session_id"]))
        assert len(approvals) == 1
        assert result["escalated"] is True
        assert "retry-from-poll" in result["output"]
    finally:
        commands.close()


@pytest.mark.parametrize("diagnostic", ["Access is denied.", "Permission denied", "syntax error", ""])
def test_ordinary_failure_does_not_request_escalation(tmp_path: Path, diagnostic: str) -> None:
    approvals = []
    launcher = DenyingLauncher(diagnostic=diagnostic, audited=False)
    context = command_context(tmp_path, launcher, lambda *args: approvals.append(args) or True)
    commands = WorkspaceCommand(tmp_path)
    try:
        with pytest.raises(CommandError):
            commands.run_with_context(context, "echo should-not-retry")
        assert approvals == []
    finally:
        commands.close()


def test_cancel_during_approval_never_starts_retry(tmp_path: Path) -> None:
    cancelled = Event()
    context = command_context(tmp_path, DenyingLauncher(), lambda *_args: cancelled.set() or True)
    context = replace(context, cancel_requested=cancelled.is_set)
    commands = WorkspaceCommand(tmp_path)
    try:
        with pytest.raises(Exception, match="cancelled"):
            commands.run_with_context(context, "echo should-not-retry")
    finally:
        commands.close()


def test_escalation_requires_explicit_one_time_approval_even_with_auto_approval(monkeypatch) -> None:
    registry = interrupts.DecisionRegistry()
    monkeypatch.setattr(interrupts, "registry", registry)
    published = []

    def sink(event):
        published.append(event)
        decision_id = event["data"]["decision_id"]
        assert not registry.resolve(decision_id, {"choice": "allow_session"})
        assert not registry.resolve(decision_id, {"choice": "continue"})
        assert registry.resolve(decision_id, {"choice": "allow_once"})

    request = InterruptRequest(
        "tool",
        "escalation",
        {
            "approval_kind": "sandbox_escalation",
            "session_id": "test",
            "command": "echo test",
        },
    )
    decide = interrupts.make_interactive_interrupt(sink, auto_approve_tools=True)
    assert decide(request).choice == "continue"
    assert len(published) == 1
    assert published[0]["data"]["approval_kind"] == "sandbox_escalation"
    assert interrupts.auto_approve(request).choice == "deny"


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("PRAXIS_NATIVE_ESCALATION_TEST") != "1",
    reason="requires explicit opt-in and a healthy installed Windows Broker",
)
@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("child", [False, True])
def test_native_windows_denial_then_approved_host_retry(tmp_path: Path, allow: bool, child: bool) -> None:
    from backend.sandbox import SandboxLauncher
    from backend.sandbox.control.broker import WindowsBrokerClient

    broker = WindowsBrokerClient.from_system()
    status = broker.status()
    if not status.healthy:
        pytest.fail(f"Native Broker unavailable: {status.code}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    target = private / "proof.txt"
    target.write_text("native-host-retry-proof", encoding="utf-8")
    scopes = []

    class RecordingLauncher(SandboxLauncher):
        def command_audit(self, process):
            scope = super().command_audit(process)
            scopes.append(scope)
            return scope

    launcher = RecordingLauncher(broker=broker, lease_store_path=tmp_path / "leases.json")
    requests = []
    context = command_context(workspace, launcher, lambda *args: requests.append(args) or allow)
    commands = WorkspaceCommand(workspace, terminal_type="pwsh")
    command = (
        "Get-Content -LiteralPath '"
        + str(target).replace("'", "''")
        + "' -ErrorAction SilentlyContinue 2>$null; if (-not $?) { exit 1 }"
    )
    if child:
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        command = (
            "& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -EncodedCommand "
            + encoded
            + "; exit $LASTEXITCODE"
        )
    try:
        if allow:
            result = json.loads(commands.run_with_context(context, command))
            assert result["escalated"] is True
            assert "native-host-retry-proof" in result["output"]
        else:
            with pytest.raises(CommandError) as failed:
                commands.run_with_context(context, command)
            assert requests, failed.value.tool_output
            assert failed.value.tool_output == requests[0][2]
            payload = json.loads(failed.value.tool_output)
            assert not payload["output"].strip()
            assert payload["permission_denials"][0]["event_id"] == 4656
        assert len(requests) == 1
        evidence = json.loads(requests[0][2])["permission_denials"]
        target_events = [entry for entry in evidence if entry["object_name"].casefold() == str(target).casefold()]
        assert target_events
        assert all((entry["process_id"] != scopes[0].root_pid) == child for entry in target_events)
        assert not launcher._leases
    finally:
        commands.close()


def test_second_permission_failure_is_final(tmp_path: Path) -> None:
    requests = []
    context = command_context(tmp_path, DenyingLauncher(), lambda *args: requests.append(args) or True)
    command = "echo Access is denied. 1>&2 & exit /b 9"
    if os.name != "nt":
        command = "echo 'Access is denied.' >&2; exit 9"
    commands = WorkspaceCommand(tmp_path)
    try:
        with pytest.raises(CommandError) as failed:
            commands.run_with_context(context, command)
        assert json.loads(failed.value.tool_output)["exit_code"] == 9
        assert len(requests) == 1
    finally:
        commands.close()


def test_timeout_does_not_escalate_even_after_a_permission_diagnostic(tmp_path: Path) -> None:
    requests = []
    context = command_context(tmp_path, DenyingLauncher(delay=3), lambda *args: requests.append(args) or True)
    context = replace(
        context, sandbox_decision=replace(context.sandbox_decision, limits=ResourceLimits(wall_seconds=1))
    )
    commands = WorkspaceCommand(tmp_path)
    try:
        with pytest.raises(CommandError, match="timed out"):
            commands.run_with_context(context, "echo should-not-retry")
        assert requests == []
    finally:
        commands.close()


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("PRAXIS_NATIVE_ESCALATION_TEST") != "1",
    reason="requires explicit opt-in and a healthy installed Windows Broker",
)
def test_native_windows_concurrent_commands_have_separate_audit_records(tmp_path: Path) -> None:
    from backend.jobs import JobRegistry
    from backend.sandbox import SandboxLauncher
    from backend.sandbox.control.broker import WindowsBrokerClient

    registry = JobRegistry()

    def run(index):
        directory = tmp_path / str(index)
        directory.mkdir()
        workspace = directory / "workspace"
        workspace.mkdir()
        target = directory / "private.txt"
        target.write_text("private", encoding="utf-8")
        requests = []
        launcher = SandboxLauncher(broker=WindowsBrokerClient.from_system(), lease_store_path=directory / "leases.json")
        commands = WorkspaceCommand(workspace, terminal_type="pwsh")
        context = command_context(workspace, launcher, lambda *args: requests.append(args) or False)
        context = replace(context, job_scope=registry.root_scope())
        command = (
            "Start-Sleep -Milliseconds 500; Get-Content -LiteralPath '"
            + str(target)
            + "' -ErrorAction SilentlyContinue 2>$null; if (-not $?) { exit 1 }"
        )
        try:
            with pytest.raises(CommandError):
                commands.run_with_context(context, command)
            assert len(requests) == 1
            return target, json.loads(requests[0][2])["permission_denials"]
        finally:
            commands.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    for index, (target, records) in enumerate(results):
        other_target = str(results[1 - index][0]).casefold()
        assert any(entry["object_name"].casefold() == str(target).casefold() for entry in records)
        assert not any(entry["object_name"].casefold() == other_target for entry in records)
    assert {entry["logon_id"] for entry in results[0][1]}.isdisjoint(entry["logon_id"] for entry in results[1][1])
