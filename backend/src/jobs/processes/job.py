"""One-shot subprocess job carrier built on :class:`ProcessGroup`.

A :class:`SubprocessJob` launches a single explicitly-specified command line
and drives it to a terminal state from a daemon monitor thread: exit ``0``
becomes ``succeeded``, a non-zero exit or a launch failure becomes ``failed``,
a timeout terminates the whole process tree and fails, and a requested
cancellation terminates the tree and is marked ``cancelled``.

It is intentionally neutral: it never reads ``os.environ``, never stores or
echoes command lines or environments, keeps captured output out of
``JobInfo.error``, and depends only on the standard library and the jobs
package.
"""

from __future__ import annotations

import codecs
import subprocess
import threading
from collections.abc import Mapping, Sequence

from backend.domain import error_report

from ..base import Job, JobKind, JobStateError
from ..output import CommandError, format_command_output
from ..output_buffer import OutputBuffer
from .group import ProcessFactory, ProcessGroup, TreeTerminator

__all__ = ["SubprocessJob"]


class SubprocessJob(Job):
    """A single spawned command line driven to a terminal lifecycle state.

    Args:
        job_id: Stable identifier for the job.
        argv: Command line (argv style) to launch.
        env: Explicit environment for the child. Always supplied by the caller;
            never read from ``os.environ``.
        cwd: Working directory for the child.
        timeout_seconds: Deadline for the process to exit before the whole tree
            is terminated and the job is marked failed. ``None`` disables it.
        max_output_chars: Character budget for the truncated output snapshot.
        popen_factory, tree_terminator, is_windows, termination_timeout:
            Optional injectables forwarded to :class:`ProcessGroup`.
        error_formatter: ``ErrorFormatter`` for ``JobInfo.error``; defaults to
            :class:`~backend.jobs.ClassNameErrorFormatter`. Pass
            :class:`MessageErrorFormatter` to surface the WorkspaceCommand
            compatible result messages verbatim.
    """

    kind = JobKind.SUBPROCESS

    def __init__(
        self,
        job_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_seconds: float | None,
        *,
        max_output_chars: int = 20_000,
        interactive: bool = False,
        command_lease=None,
        popen_factory: ProcessFactory = subprocess.Popen,
        tree_terminator: TreeTerminator | None = None,
        is_windows: bool | None = None,
        termination_timeout: float = 5.0,
        error_formatter=None,
        clock=None,
        listener=None,
        sandbox_policy=None,
        sandbox_launcher=None,
        resource_monitor=None,
    ) -> None:
        super().__init__(
            job_id,
            self.kind,
            clock=clock,
            error_formatter=error_formatter,
            listener=listener,
        )
        self.interactive = interactive
        self.command_lease = command_lease
        self.buffer = OutputBuffer()
        self.release_callback = None
        self.read_position = 0
        self.interaction_lock = threading.Lock()
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._group = ProcessGroup(
            argv,
            env,
            cwd,
            is_windows=is_windows,
            popen_factory=popen_factory,
            tree_terminator=tree_terminator,
            termination_timeout=termination_timeout,
            retain_tree=interactive,
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._monitor_thread: threading.Thread | None = None
        self._output = ""
        self._stdout = ""
        self._stderr = ""
        self.sandbox_policy = sandbox_policy
        self.sandbox_launcher = sandbox_launcher
        self.resource_monitor = resource_monitor
        if sandbox_policy is not None:
            raw = sandbox_policy.to_dict()
            self._set_sandbox_info(
                {
                    "enforced": bool(raw.get("enforced", True)),
                    "file_mode": raw.get("file_mode", "read_only"),
                    "network_mode": raw.get("network_mode", "no_network"),
                    "limits": raw.get("limits", {}),
                    "failure_code": None,
                    "cleanup_pending": False,
                }
            )

    # -- public API ---------------------------------------------------------

    @property
    def output_limit(self) -> int:
        return self._max_output_chars

    @property
    def output(self) -> str:
        """Truncated ``stdout``/``stderr`` snapshot rendered via
        :func:`format_command_output`; captured output never enters
        ``JobInfo.error``."""
        return self._output

    @property
    def stdout(self) -> str:
        """The decoded (untrucated) captured standard output."""
        return self._stdout

    @property
    def stderr(self) -> str:
        """The decoded (untrucated) captured standard error."""
        return self._stderr

    def _mark_failed(self, exception: BaseException, *, exit_code: int | None = None, pids=()) -> None:
        if self._output:
            exception.add_note(
                format_command_output(
                    self._stdout, self._stderr, max_chars=max(0, self._max_output_chars - len(str(exception)) - 1)
                )
            )
        super()._mark_failed(exception, exit_code=exit_code, pids=pids)

    def start(self) -> None:
        """Launch the child and begin monitoring it.

        Calls ``super().start()`` first; a launch failure (the injected factory
        raising ``FileNotFoundError``/``OSError``) marks the job failed and is
        re-raised so the registry can propagate it.
        """
        super().start()
        try:
            pid = self._group.start()
        except OSError as exc:
            self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
            self._mark_failed(exc)
            raise
        except Exception as exc:
            self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
            self._mark_failed(exc)
            raise
        self._set_process_info((pid,))
        if self.resource_monitor is not None:
            self.resource_monitor.on_exceeded = self._resource_exceeded
            self.resource_monitor.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name=f"job-{self._id}-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def close(self, timeout: float | None = None) -> None:
        """Cancel and wait, then join the monitor thread (idempotent)."""
        super().close(timeout)
        thread = self._monitor_thread
        if thread is not None:
            thread.join(timeout=5.0)

    def release_output(self) -> None:
        self.buffer.clear()
        if self.release_callback is not None:
            callback, self.release_callback = self.release_callback, None
            callback()

    def _request_cancel(self) -> None:
        """Stop the running subprocess by terminating the whole tree; the
        monitor thread observes the exit and seals the ``cancelled`` state."""
        self._group.terminate()

    def _mark_sandbox_failure(self, code: object) -> None:
        current = self.info().sandbox
        if current is None:
            return
        current.update({"failure_code": str(code)})
        self._set_sandbox_info(current)

    # -- monitor thread -----------------------------------------------------

    def _monitor(self) -> None:
        outcome: tuple[str, int | None, Exception | None] = ("failed", None, CommandError("Command failed."))
        try:
            try:
                stdout, stderr = self._communicate()
            except subprocess.TimeoutExpired as timeout_error:
                self._group.terminate()
                stdout, stderr = (b"", b"") if self.interactive else self._group.communicate(timeout=30.0)
                exit_code = self._group.poll()
                self._capture(stdout, stderr)
                if self.info().cancel_requested_at is not None:
                    outcome = ("cancelled", exit_code, None)
                else:
                    failure = CommandError(f"Command timed out after {self._timeout_seconds} seconds.")
                    failure.__cause__ = timeout_error
                    outcome = ("failed", exit_code, failure)
            else:
                exit_code = self._group.poll()
                self._capture(stdout, stderr)
                if self.info().cancel_requested_at is not None:
                    outcome = ("cancelled", exit_code, None)
                elif exit_code == 0:
                    outcome = ("succeeded", 0, None)
                else:
                    outcome = ("failed", exit_code, CommandError(f"Command exited with code {exit_code}."))
        except JobStateError:
            # A concurrent cancel/close already sealed the terminal state.
            return
        except Exception as exc:
            self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
            # A failed Broker request cannot supply a reliable exit code.
            outcome = ("failed", None, exc)
        finally:
            cleanup_errors: list[Exception] = []
            if self.resource_monitor is not None:
                try:
                    self.resource_monitor.stop()
                except Exception as exc:
                    cleanup_errors.append(exc)
            if self.interactive:
                try:
                    self._group.release_streams()
                except Exception as exc:
                    cleanup_errors.append(exc)
            if self.sandbox_launcher is not None:
                process = getattr(self._group, "_process", None)
                try:
                    if not self.sandbox_launcher.cleanup(process):
                        cleanup_errors.append(CommandError("Sandbox cleanup failed."))
                except Exception as exc:
                    cleanup_errors.append(exc)
            if self.command_lease is not None:
                try:
                    self.command_lease.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                self.command_lease = None
            if cleanup_errors:
                current = self.info().sandbox or {}
                current.update({"cleanup_pending": True, "failure_code": "sandbox_cleanup_failed"})
                self._set_sandbox_info(current)
                original = outcome[2]
                if original is None:
                    original = cleanup_errors.pop(0)
                    outcome = ("failed", outcome[1], original)
                for cleanup_error in cleanup_errors:
                    original.add_note("Cleanup also failed:\n" + error_report(cleanup_error)["traceback"])

        kind, exit_code, error = outcome
        try:
            if kind == "cancelled":
                self._mark_cancelled(exit_code=exit_code)
            elif kind == "succeeded":
                self._mark_succeeded(exit_code=0)
            else:
                self._mark_failed(error or CommandError("Command failed."), exit_code=exit_code)
        except JobStateError:
            return

    def _communicate(self) -> tuple[bytes | None, bytes | None]:
        if not self.interactive:
            return self._group.communicate(timeout=self._timeout_seconds)
        errors: list[Exception] = []

        def drain(source: str) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while data := self._group.read_stream(source):
                    self.buffer.append(decoder.decode(data).encode("utf-8"), source)
                self.buffer.append(decoder.decode(b"", final=True).encode("utf-8"), source)
            except Exception as exc:
                errors.append(exc)
                try:
                    self._group.terminate()
                except Exception as cleanup:
                    exc.add_note(
                        "Stopping command after output failure also failed:\n" + error_report(cleanup)["traceback"]
                    )

        readers = [threading.Thread(target=drain, args=(source,), daemon=True) for source in ("stdout", "stderr")]
        for reader in readers:
            reader.start()
        code = self._group.wait(self._timeout_seconds)
        if code is None:
            self._group.terminate()
        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            self._group.terminate()
            for reader in readers:
                reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise OSError("Command output reader did not exit after the process stopped.")
        if errors:
            raise errors[0]
        if code is None:
            raise subprocess.TimeoutExpired("command", self._timeout_seconds)
        return b"", b""

    def write_input(self, chars: str) -> None:
        if chars == "\x03":
            self._group.interrupt()
        elif chars:
            self._group.write_stdin(chars.encode("utf-8"))

    def _resource_exceeded(self, error: Exception) -> None:
        self._mark_sandbox_failure("resource_exceeded")
        self._group.terminate()

    def _finish_timeout(self, exit_code: int | None) -> None:
        self._mark_failed(
            CommandError(f"Command timed out after {self._timeout_seconds} seconds."),
            exit_code=exit_code,
        )

    def _capture(self, stdout: bytes | None, stderr: bytes | None) -> None:
        self._stdout = _as_text(stdout) if stdout else ""
        self._stderr = _as_text(stderr) if stderr else ""
        self._output = format_command_output(stdout, stderr, max_chars=self._max_output_chars)


def _as_text(value: bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if value else ""
