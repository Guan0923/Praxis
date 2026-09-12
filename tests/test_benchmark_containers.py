"""Real Docker/tool/runtime checks, explicitly opt-in and never paid model calls."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import replace
from threading import Event, Thread, Timer

import httpx
import pytest
import uvicorn

from backend.runtime import RunnerSettings
from backend.runtime.core.context.state import RuntimeState
from benchmarks.containers import ContainerCancelled, ContainerTimeout, TaskContainer, cache_root, command
from benchmarks.tasks import TASKS_BY_NAME
from tests.benchmark_local_support import acceptance_app, local_model
from tests.test_benchmark_service import until


def test_unlimited_tool_budget_round_trips_without_changing_default():
    state = RuntimeState(session_id="unlimited", runner_settings=RunnerSettings(max_tool_calls=None))
    assert RuntimeState.from_dict(state.to_dict()).runner_settings.max_tool_calls is None
    assert RunnerSettings().max_tool_calls == 512


def test_cli_timeout_terminates_its_child():
    import sys

    with pytest.raises(ContainerTimeout):
        command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)


def test_unlimited_command_completes_and_can_still_be_cancelled():
    import sys

    code, output = command([sys.executable, "-c", "print('finished')"], timeout=None)
    assert code == 0 and output.strip() == "finished"
    cancelled = Event()
    timer = Timer(0.2, cancelled.set)
    timer.start()
    try:
        with pytest.raises(ContainerCancelled):
            command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=None, cancelled=cancelled.is_set)
    finally:
        timer.cancel()
        timer.join()


@pytest.mark.skipif(
    os.environ.get("PRAXIS_TEST_DOCKER") != "1", reason="Set PRAXIS_TEST_DOCKER=1 for real Docker tests"
)
def test_real_container_files_are_isolated_and_stop_terminates_execution():
    original = TASKS_BY_NAME["tb2-log-summary-date-ranges"]
    first, second = TaskContainer(original), TaskContainer(original)
    try:
        first.start()
        second.start()
        first.write("/app/isolated.txt", "first")
        code, _ = second.exec("test -e /app/isolated.txt", check=False)
        assert code != 0
        _, config = first.docker("inspect", first.name)
        inspected = json.loads(config)[0]
        assert inspected["Mounts"] == []
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert "UV_OFFLINE=1" in inspected["Config"]["Env"]
        cancelled = Event()
        first.cancelled = cancelled.is_set
        errors = []

        def execute():
            try:
                first.exec("sleep 30")
            except Exception as exc:
                errors.append(exc)

        worker = Thread(target=execute)
        worker.start()
        cancelled.set()
        worker.join(10)
        assert not worker.is_alive() and errors
        code, _ = command(["docker", "inspect", first.name], check=False)
        assert code != 0
        assert second.exec("printf STILL_RUNNING")[1] == "STILL_RUNNING"
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(
    os.environ.get("PRAXIS_TEST_DOCKER") != "1", reason="Set PRAXIS_TEST_DOCKER=1 for real Docker tests"
)
@pytest.mark.parametrize(
    ("name", "solved"),
    [
        ("tb2-log-summary-date-ranges", True),
        ("tb2-cancel-async-tasks", True),
        ("swepro-ansible__ansible-a26c325bd8f6e2822d9d7e62f77a424c1db4fbf6", True),
        ("tb2-cancel-async-tasks", False),
    ],
)
def test_public_task_real_http_runtime_container_and_upstream_grading(name, solved, tmp_path, monkeypatch):
    import benchmarks.tasks

    task = TASKS_BY_NAME[name]
    if task.container["kind"] == "terminal_bench":
        solution = (
            cache_root() / "terminal-bench-2" / task.container["task_path"] / "solution" / "solve.sh"
        ).read_text(encoding="utf-8")
    else:
        row = json.loads((cache_root() / "swe-selected.json").read_text())[task.source.task_id]
        solution = "git apply --allow-empty <<'LOCAL_REFERENCE_PATCH'\n" + row["patch"] + "\nLOCAL_REFERENCE_PATCH\n"
    if not solved:
        solution = "pwd"
    monkeypatch.setattr(benchmarks.tasks, "ALL_TASKS", (task,))
    monkeypatch.setattr(benchmarks.tasks, "TASKS_BY_NAME", {task.name: task})
    with local_model(tool_name="container_exec", tool_arguments={"command": solution, "timeout_seconds": 900}) as (
        config,
        calls,
    ):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(acceptance_app(tmp_path / "web", config), log_level="error"))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            until(lambda: server.started)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                response = client.post("/benchmark/run", json={"task": task.name})
                assert response.status_code == 202
                identifier = response.json()["id"]
                until(lambda: client.get(f"/benchmark/runs/{identifier}").json()["finished"] == 1, timeout=180)
                run = client.get(f"/benchmark/runs/{identifier}").json()
                result = run["tasks"][0]["result"]
                assert run["status"] == "completed", result.get("error") or result
                assert result["passed"] is solved, result
                assert result["score"] == float(solved), result
                assert result["metrics"]["tool_calls"] == 1
                assert len(calls) == 2
        finally:
            server.should_exit = True
            worker.join(15)
            listener.close()
            assert not worker.is_alive()


@pytest.mark.skipif(
    os.environ.get("PRAXIS_TEST_DOCKER") != "1", reason="Set PRAXIS_TEST_DOCKER=1 for real Docker tests"
)
def test_real_runtime_reports_environment_errors(tmp_path):
    from benchmarks.runner import run_one_task
    from benchmarks.sandbox import Sandbox

    original = TASKS_BY_NAME["tb2-log-summary-date-ranges"]
    task = replace(original, container={**original.container, "source_image_id": "sha256:wrong-image"})
    with local_model(tool_name="container_exec", tool_arguments={"command": "sleep 30"}) as (config, calls):
        sandbox = Sandbox(tmp_path / "environment", model_config=config)
        sandbox.prepare()
        result = run_one_task(task, planner="llm", sandbox=sandbox)
    assert result.status == "error"
    assert result.failure_phase == "environment"
    assert result.score is None
    assert not calls


@pytest.mark.skipif(
    os.environ.get("PRAXIS_TEST_DOCKER") != "1", reason="Set PRAXIS_TEST_DOCKER=1 for real Docker tests"
)
@pytest.mark.parametrize("task", list(TASKS_BY_NAME.values()), ids=lambda task: task.name)
def test_every_public_task_hides_grading_material_and_host_access(task):
    from benchmarks.upstream import prepare_environment

    container = TaskContainer(task)
    try:
        prepare_environment(container)
        _, text = container.docker("inspect", container.name)
        inspected = json.loads(text)[0]
        assert inspected["Mounts"] == []
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert inspected["HostConfig"]["Memory"] <= 4 * 1024**3
        container.exec("test ! -e /tests && test ! -e /solution && test ! -S /var/run/docker.sock")
        if task.container["kind"] == "swe_bench_pro":
            assert container.exec("git rev-list --all --count")[1].strip() == "1"
    finally:
        container.close()
