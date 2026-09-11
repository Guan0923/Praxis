"""Workspace-rooted cross-platform command execution."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TERMINAL_LABELS, TerminalType, normalize_terminal_type
from backend.jobs import (
    AdmissionPolicy,
    JobLane,
    JobRegistry,
    JobScope,
    JobScopeKind,
    JobState,
    MessageErrorFormatter,
    ProcessFactory,
    SlotMode,
    SubprocessJob,
    TreeTerminator,
)
from backend.sandbox import (
    SandboxExecutionDecision,
    TerminalKind,
)

from .base import ToolError, ToolInvocationContext
from .terminal import terminal_executable, windows_workspace_to_wsl


class WorkspaceCommand:
    """Run an explicitly approved command with the workspace as its working directory."""

    _MAX_OUTPUT_CHARS = 20_000
    _SENSITIVE_ENV_COMPOUNDS = (
        "ACCESS_KEY",
        "API_KEY",
        "PRIVATE_KEY",
    )
    _SENSITIVE_ENV_SEGMENTS = {
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWD",
        "PASSWORD",
        "PAT",
        "SECRET",
        "TOKEN",
    }

    def __init__(
        self,
        workspace: Path,
        *,
        is_windows: bool | None = None,
        terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
        popen_factory: ProcessFactory | None = None,
        tree_terminator: TreeTerminator | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._sessions: dict[str, tuple[SubprocessJob, ToolInvocationContext, JobRegistry | None]] = {}
        self._session_lock = RLock()
        self._workspace = workspace.resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._terminal_type = normalize_terminal_type(terminal_type)
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
        self._environment = self._filtered_environment(os.environ if environment is None else environment)

    def run(self, cmd: str, yield_time_ms: int = 10000, max_output_tokens: int = 2000) -> str:
        return self.run_with_context(ToolInvocationContext(), cmd, yield_time_ms, max_output_tokens)

    def run_with_context(
        self, context: ToolInvocationContext, cmd: str, yield_time_ms: int = 10000, max_output_tokens: int = 2000
    ) -> str:
        self._validate_request(yield_time_ms, max_output_tokens)
        if not isinstance(cmd, str) or not cmd.strip():
            raise ToolError("cmd must be a non-empty string.")
        self._prune()
        if context.cancel_requested is not None and context.cancel_requested():
            raise ToolError("Command was cancelled before start.")

        parent_scope, private_registry = self._resolve_scope(context)
        job: SubprocessJob | None = None
        try:
            task_scope = parent_scope.child(
                JobScopeKind.TASK,
                parent_job_id=parent_scope.parent_job_id,
            )
            max_output_chars = self._MAX_OUTPUT_CHARS
            job_options: dict[str, Any] = {
                "max_output_chars": max_output_chars,
                "tree_terminator": self._tree_terminator,
                "is_windows": self._is_windows,
                "error_formatter": MessageErrorFormatter(),
            }
            if self._popen_factory is not None:
                job_options["popen_factory"] = self._popen_factory
            job_id = parent_scope.registry.new_job_id()
            effective_timeout = None
            decision = context.sandbox_decision
            if isinstance(decision, SandboxExecutionDecision):
                if self._terminal_type == "wsl":
                    raise ToolError("WSL is disabled for sandboxed run_command execution.")
                policy = decision.command_policy(job_id, TerminalKind(self._terminal_type))
                max_output_chars = decision.limits.output_chars
                job_options["max_output_chars"] = max_output_chars
                effective_timeout = decision.limits.wall_seconds
                sandbox_user_id = decision.user_id or "local"
                job_options["popen_factory"] = decision.launcher.popen_factory(
                    policy,
                    user_id=sandbox_user_id,
                    job_kind="command",
                )
                job_options["tree_terminator"] = decision.launcher.terminate_tree
                job_options["sandbox_policy"] = policy
                job_options["sandbox_launcher"] = decision.launcher
            job = SubprocessJob(
                job_id,
                self._command_line(cmd),
                self._environment,
                str(self._workspace),
                effective_timeout,
                interactive=True,
                **job_options,
            )
            if isinstance(decision, SandboxExecutionDecision):
                decision.launcher.wait_resources(
                    job_id,
                    cancelled=context.cancel_requested,
                    notify=context.resource_wait,
                )
            try:
                if context.cancel_requested is not None and context.cancel_requested():
                    raise InterruptedError("Command cancelled before launch.")
                task_scope.submit(
                    job,
                    lane=JobLane.FOREGROUND,
                    admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
                )
                if isinstance(decision, SandboxExecutionDecision) and not job.info().pids:
                    raise InterruptedError("Command cancelled before launch.")
            except BaseException as original:
                if isinstance(decision, SandboxExecutionDecision):
                    try:
                        decision.launcher.cancel_resource_wait(job_id)
                    except Exception as cleanup:
                        original.add_note(f"Resource wait cleanup failed: {cleanup}")
                raise
            with self._session_lock:
                self._sessions[job_id] = (job, context, private_registry)
                job.release_callback = lambda: self._release(job_id)
            return self._read(job, context, yield_time_ms, max_output_tokens)
        except BaseException:
            if private_registry is not None:
                private_registry.close_all(reason="command start failed", timeout=5.0)
            raise

    def write_stdin(
        self, session_id: str, chars: str = "", yield_time_ms: int = 60000, max_output_tokens: int = 2000
    ) -> str:
        return self.write_with_context(ToolInvocationContext(), session_id, chars, yield_time_ms, max_output_tokens)

    def write_with_context(
        self,
        context: ToolInvocationContext,
        session_id: str,
        chars: str = "",
        yield_time_ms: int = 60000,
        max_output_tokens: int = 2000,
    ) -> str:
        self._validate_request(yield_time_ms, max_output_tokens)
        self._prune()
        with self._session_lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise ToolError("Command session does not exist or has been released.")
        job, owner, _registry = entry
        if (owner.session_id, owner.turn_id, owner.job_scope) != (
            context.session_id,
            context.turn_id,
            context.job_scope,
        ):
            raise ToolError("Command session belongs to another Turn or run.")
        if not isinstance(chars, str) or len(chars.encode("utf-8")) > 16384:
            raise ToolError("chars must be a string of at most 16 KiB.")
        unregister = context.register_abort(lambda: job.cancel("Turn cancelled")) if context.register_abort else None
        try:
            with job.interaction_lock:
                if isinstance(owner.job_scope, JobScope) and owner.job_scope.closed:
                    raise ToolError("Command session has been released.")
                job.write_input(chars)
                return self._read(job, context, yield_time_ms, max_output_tokens)
        finally:
            if unregister is not None:
                unregister()

    def _release(self, job_id: str) -> None:
        with self._session_lock:
            self._sessions.pop(job_id, None)

    def _prune(self) -> None:
        with self._session_lock:
            expired = [
                key
                for key, (_job, owner, _registry) in self._sessions.items()
                if isinstance(owner.job_scope, JobScope) and owner.job_scope.closed
            ]
            for key in expired:
                job, _owner, _registry = self._sessions.pop(key)
                job.buffer.clear()

    def close(self) -> None:
        with self._session_lock:
            entries = list(self._sessions.values())
            self._sessions.clear()
        for job, _owner, registry in entries:
            job.close(timeout=5)
            job.buffer.clear()
            if registry is not None:
                registry.close_all(reason="command manager closed", timeout=5)

    @staticmethod
    def _validate_request(yield_time_ms: int, max_output_tokens: int) -> None:
        if isinstance(yield_time_ms, bool) or not isinstance(yield_time_ms, int) or not 0 <= yield_time_ms <= 300000:
            raise ToolError("yield_time_ms must be an integer between 0 and 300000.")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
            raise ToolError("max_output_tokens must be a positive integer.")

    def _read(self, job: SubprocessJob, context: ToolInvocationContext, wait_ms: int, tokens: int) -> str:
        deadline = time.monotonic() + wait_ms / 1000
        while not job.wait(min(0.05, max(0, deadline - time.monotonic()))):
            if context.cancel_requested is not None and context.cancel_requested():
                job.cancel("Turn cancelled")
            if time.monotonic() >= deadline:
                break
        output, job.read_position, cache_omitted = job.buffer.read_details(job.read_position)
        from .command_output import limit_output

        info = job.info()
        bounded_output, response_omitted = limit_output(output, tokens, min(self._MAX_OUTPUT_CHARS, job.output_limit))
        truncated = bool(cache_omitted or response_omitted)
        del output
        result: dict[str, Any] = {"output": bounded_output, "status": info.state.value, "output_truncated": truncated}
        if cache_omitted:
            result["cache_omitted_bytes"] = cache_omitted
        if response_omitted:
            result["response_omitted_bytes"] = response_omitted
        if info.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            result["exit_code"] = info.exit_code
        else:
            result["session_id"] = info.id
        if info.state in {JobState.FAILED, JobState.CANCELLED}:
            from backend.domain import error_report

            failure = job.failure_exception or ToolError("Command was cancelled.")
            result["error_report"] = error_report(failure)
            failure.tool_output = json.dumps(result, ensure_ascii=False)
            raise failure
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _resolve_scope(context: ToolInvocationContext) -> tuple[JobScope, JobRegistry | None]:
        if context.job_scope is not None:
            if not isinstance(context.job_scope, JobScope):
                raise ToolError("The command process manager context is invalid.")
            return context.job_scope, None

        registry = JobRegistry()
        runner_scope = registry.root_scope().child(JobScopeKind.THREAD)
        run_scope = runner_scope.child(JobScopeKind.RUN, session_id=context.session_id)
        return run_scope, registry

    def _command_line(self, command: str) -> list[str]:
        if not self._is_windows:
            return ["bash", "-c", command]

        executable = terminal_executable(self._terminal_type, environment=self._environment)
        if executable is None:
            raise ToolError(f"{TERMINAL_LABELS[self._terminal_type]} is not available on this system.")
        if self._terminal_type == "cmd":
            return [executable, "/d", "/s", "/c", command]
        if self._terminal_type in {"powershell", "pwsh"}:
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WorkingDirectory",
                str(self._workspace),
                "-Command",
                command,
            ]
        if self._terminal_type == "git_bash":
            return [executable, "-lc", command]
        try:
            linux_workspace = windows_workspace_to_wsl(self._workspace)
        except ValueError:
            raise
        return [executable, "--cd", linux_workspace, "--", "sh", "-lc", command]

    @classmethod
    def _filtered_environment(cls, environment: Mapping[str, str]) -> dict[str, str]:
        return {name: value for name, value in environment.items() if not cls._is_sensitive_environment_name(name)}

    @classmethod
    def _is_sensitive_environment_name(cls, name: str) -> bool:
        normalised = name.upper()
        if any(compound in normalised for compound in cls._SENSITIVE_ENV_COMPOUNDS):
            return True
        return any(segment in cls._SENSITIVE_ENV_SEGMENTS for segment in normalised.split("_"))
