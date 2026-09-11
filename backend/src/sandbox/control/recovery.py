"""Start stopped services before considering the existing repair transaction."""

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .broker import BrokerStatus, WindowsBrokerClient


def recover(
    client: "WindowsBrokerClient",
    *,
    before_repair: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> "BrokerStatus":
    installer = client._installer
    if installer is None:
        failure = getattr(client, "_initialization_error", None) or RuntimeError(
            "Windows service controller is unavailable"
        )
        failure.broker_recovery_code = "broker_service_state_failed"
        raise failure

    def query() -> dict[str, Any]:
        try:
            return installer.service_state()
        except Exception as exc:
            exc.broker_recovery_code = "broker_service_state_failed"
            raise

    def failed_start(state: dict[str, Any]) -> None:
        raise RuntimeError(
            f"Sandbox service did not start: state={state['state']}, "
            f"exit_code={state['exit_code']}, service_exit_code={state['service_exit_code']}"
        )

    def health(deadline: float | None = None) -> "BrokerStatus":
        current = client.status() if deadline is None else client.status(timeout=max(0, min(1, deadline - clock())))
        if current.code == "broker_service_state_failed":
            failure = RuntimeError(current.detail or "Sandbox service state query failed")
            failure.error_report = current.error_report
            failure.broker_recovery_code = "broker_service_state_failed"
            raise failure
        if current.service_state not in {None, "running"}:
            failed_start(query())
        return current

    try:
        state = query()
        deadline = clock() + 10
        was_starting = state["state"] == "start_pending"
        while state["state"] in {"start_pending", "stop_pending"}:
            if clock() >= deadline:
                raise TimeoutError(f"Sandbox service remained {state['state']} for 10 seconds")
            sleep(min(1, max(0, deadline - clock())))
            state = query()
        if was_starting and state["state"] != "running":
            failed_start(state)
        if state["state"] not in {"stopped", "running", "missing"}:
            failed_start(state)
        observed_start = was_starting or state["state"] == "stopped"
        if state["state"] == "stopped":
            installer.start()
            deadline = clock() + 10
            while True:
                state = query()
                if state["state"] == "running":
                    current = health(deadline)
                    if current.healthy:
                        return current
                elif state["state"] != "start_pending":
                    failed_start(state)
                if clock() >= deadline:
                    if state["state"] != "running":
                        raise TimeoutError("Sandbox service did not reach running state within 10 seconds")
                    break
                sleep(min(1, max(0, deadline - clock())))
        elif was_starting:
            deadline = clock() + 10
            while not (current := health(deadline)).healthy:
                state = query()
                if state["state"] != "running":
                    failed_start(state)
                if clock() >= deadline:
                    break
                sleep(min(1, max(0, deadline - clock())))
            else:
                return current
        if not observed_start and state["state"] != "missing" and (current := health()).healthy:
            return current
    except Exception as exc:
        if not getattr(exc, "broker_recovery_code", None):
            exc.broker_recovery_code = "broker_service_start_failed"
        raise

    if state["state"] == "missing":
        if before_repair is not None:
            before_repair()
        return client._status_command("install")
    if before_repair is not None:
        before_repair()
    return client._status_command("repair")
