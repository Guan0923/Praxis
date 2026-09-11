from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import Any

from backend.sandbox import SandboxLauncher
from backend.sandbox.control.maintenance import SandboxMaintenanceGate


class DirectTestSandboxLauncher(SandboxLauncher):
    """Test-only process launcher for tests that do not exercise isolation."""

    def __init__(self) -> None:
        self.maintenance_gate = SandboxMaintenanceGate()
        self.policies: list[Any] = []
        self._temp_dirs: dict[int, object] = {}
        self._leases = {}

    def command_lease(self):
        return self.maintenance_gate.acquire_command()

    def wait_resources(self, ticket, *, cancelled=None, notify=None):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")

    def cancel_resource_wait(self, ticket):
        pass

    def popen_factory(self, policy, **_options):
        self.policies.append(policy)

        def factory(argv: Sequence[str], **kwargs: Any):
            if os.name == "nt":
                from backend.sandbox.native_broker_adapter.process import _windows_command_line

                process = subprocess.Popen(_windows_command_line(list(argv)), **kwargs)
            else:
                process = subprocess.Popen(argv, **kwargs)
            self._leases[process.pid] = self.command_lease()
            return process

        return factory

    @staticmethod
    def terminate_tree(process: Any) -> None:
        process.terminate()

    def cleanup(self, process_or_pid: Any) -> bool:
        pid = process_or_pid if isinstance(process_or_pid, int) else getattr(process_or_pid, "pid", None)
        lease = self._leases.pop(pid, None)
        if lease is not None:
            lease.close()
        return True
