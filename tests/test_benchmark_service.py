from __future__ import annotations

import json
from dataclasses import replace
from threading import Event, Lock
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.shared.benchmark import create_benchmark_app
from backend.jobs import JobRegistry
from backend.providers import ModelConfig
from benchmarks.metrics import RunMetrics
from benchmarks.model import Seed, SeedMcp, SeedSkill, TaskResult
from benchmarks.runner import run_one_task
from benchmarks.sandbox import Sandbox
from benchmarks.service import FINISHED, BenchmarkConflict, BenchmarkService
from benchmarks.tasks import ALL_TASKS


def result_for(task, *, passed=True):
    return TaskResult(
        task.name,
        task.capability,
        "completed",
        float(passed),
        "done",
        RunMetrics(1, 1, 1, 0, 0, 0, 0, []),
        [],
        passed=passed,
        trace=[
            {
                "type": "context",
                "session_id": "session-test",
                "thread_id": "thread-test",
                "turn_id": "turn-test",
                "data_idx": 0,
                "data": None,
            }
        ],
    )


def until(check, timeout=8):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        value = check()
        if value:
            return value
        sleep(0.01)
    raise TimeoutError("Benchmark condition did not become true.")


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    resources = []

    def create(execute):
        from benchmarks.resources import ResourceStore

        store = ResourceStore(tmp_path / f"cache-{len(resources)}")
        monkeypatch.setattr(store, "ensure_ready", lambda tasks: None)
        monkeypatch.setattr(store, "reserve", lambda tasks: None)
        registry = JobRegistry()
        service = BenchmarkService(registry, tmp_path / str(len(resources)), execute=execute, resources=store)
        resources.append((service, registry))
        return service

    yield create
    for service, registry in resources:
        service.close()
        registry.close_all(timeout=5)


def test_three_workers_queue_partial_results_and_conflicts(service_factory):
    gates = {task.name: Event() for task in ALL_TASKS}
    started = set()
    lock = Lock()

    def execute(task, *, is_cancelled=None, cancel_requested, **kwargs):
        with lock:
            started.add(task.name)
        while not gates[task.name].wait(0.01):
            if cancel_requested():
                break
        return result_for(task, passed=False)

    service = service_factory(execute)
    batch = service.start(ALL_TASKS, "llm", None)
    until(lambda: len(started) == 3)
    snapshot = service.snapshot(batch["id"])
    assert [item["status"] for item in snapshot["tasks"]].count("queued") == len(ALL_TASKS) - 3
    with pytest.raises(BenchmarkConflict):
        service.start([ALL_TASKS[0]], "llm", None)
    with pytest.raises(BenchmarkConflict):
        service.start(ALL_TASKS, "llm", None)
    gates[ALL_TASKS[0].name].set()
    until(lambda: service.snapshot(batch["id"])["finished"] == 1)
    until(lambda: len(started) == 4)
    first = service.snapshot(batch["id"])["tasks"][0]
    assert first["status"] == "completed" and first["result"]["passed"] is False
    assert "trace" not in first["result"]
    assert len(service.trace(batch["id"], first["id"])) == 1
    for gate in gates.values():
        gate.set()
    until(lambda: service.snapshot(batch["id"])["status"] == "completed")


def test_cancel_queued_and_running_waits_for_worker_exit(service_factory):
    release = Event()

    def execute(task, **kwargs):
        release.wait(5)
        return result_for(task)

    service = service_factory(execute)
    batch = service.start(ALL_TASKS, "llm", None)
    stopped = service.cancel(batch["id"])
    assert stopped["status"] == "stopping"
    assert [item["status"] for item in stopped["tasks"]].count("cancelled") == len(ALL_TASKS) - 3
    assert [item["status"] for item in stopped["tasks"]].count("stopping") == 3
    release.set()
    until(lambda: service.snapshot(batch["id"])["status"] == "cancelled")
    assert service.snapshot(batch["id"])["finished"] == len(ALL_TASKS)


def test_task_cancel_does_not_cancel_siblings_and_finished_scores_survive(service_factory):
    def execute(task, *, cancel_requested, **kwargs):
        if task == ALL_TASKS[0]:
            return result_for(task)
        until(cancel_requested)
        return result_for(task)

    service = service_factory(execute)
    batch = service.start(ALL_TASKS[:3], "llm", None)
    until(lambda: service.snapshot(batch["id"])["finished"] == 1)
    service.cancel(batch["id"], batch["tasks"][1]["id"])
    until(lambda: service.snapshot(batch["id"])["tasks"][1]["status"] == "cancelled")
    snapshot = service.snapshot(batch["id"])
    assert snapshot["tasks"][2]["status"] == "running"
    service.cancel(batch["id"])
    assert service.snapshot(batch["id"])["tasks"][0]["result"]["passed"] is True


def test_failures_are_redacted_and_do_not_stop_other_tasks(service_factory, monkeypatch):
    original = Sandbox.prepare
    count = 0
    lock = Lock()

    def prepare(self):
        nonlocal count
        with lock:
            count += 1
            if count == 1:
                raise RuntimeError("Authorization: Bearer private-test-value")
        return original(self)

    monkeypatch.setattr(Sandbox, "prepare", prepare)
    service = service_factory(lambda task, **kwargs: result_for(task))
    batch = service.start(ALL_TASKS[:4], "llm", None)
    until(lambda: service.snapshot(batch["id"])["finished"] == 4)
    snapshot = service.snapshot(batch["id"])
    assert snapshot["status"] == "failed"
    assert sum(task["status"] == "completed" for task in snapshot["tasks"]) == 3
    assert "private-test-value" not in json.dumps(snapshot)


def test_each_execution_has_isolated_mcp_skills_and_model_snapshot(service_factory):
    roots = []
    configs = []
    first = replace(ALL_TASKS[0], seed=Seed(mcp=SeedMcp("first"), skills=(SeedSkill("one", "one", "one"),)))
    second = replace(ALL_TASKS[1], seed=Seed(mcp=SeedMcp("second"), skills=(SeedSkill("two", "two", "two"),)))

    def execute(task, *, sandbox, **kwargs):
        sandbox.materialize_workspace(task)
        roots.append(sandbox.root)
        configs.append(sandbox.model_config.model)
        assert f"[servers.{task.seed.mcp.server_name}]" in sandbox.paths.mcp_file.read_text()
        assert len(list(sandbox.paths.skills_dir.iterdir())) == 1
        assert "local-only-key" not in sandbox.paths.config_file.read_text()
        assert "[sync]" not in sandbox.paths.config_file.read_text()
        return result_for(task)

    service = service_factory(execute)
    for task, model in [(first, "first-model"), (second, "second-model")]:
        service.start([task], "llm", ModelConfig("local-only-key", "http://127.0.0.1:1/v1", model))
    until(lambda: all(run["finished"] == 1 for run in service.snapshot()["runs"]))
    assert all(run["status"] == "completed" for run in service.snapshot()["runs"]), [
        item["result"]["error"] for run in service.snapshot()["runs"] for item in run["tasks"]
    ]
    assert len(set(roots)) == 2
    assert set(configs) == {"first-model", "second-model"}


def test_runner_reports_cleanup_failure(tmp_path, monkeypatch, local_sandbox_runtime):
    from backend.runtime.application.services import AgentApplication

    original = AgentApplication.close

    def close(self):
        original(self)
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(AgentApplication, "close", close)
    task = replace(
        ALL_TASKS[0], planner_modes=frozenset({"rule"}), prompt="hello", seed=Seed(), checkers=(), container=None
    )
    sandbox = Sandbox(tmp_path / "sandbox")
    sandbox.prepare()
    phases = []
    result = run_one_task(task, planner="rule", sandbox=sandbox, on_phase=phases.append)
    assert result.status == "error" and result.failure_phase == "cleanup"
    assert "cleanup failed" in result.error
    assert phases[-1] == "cleanup"


def test_api_starts_immediately_and_exposes_status_cancel_and_missing(service_factory):
    def execute(task, *, cancel_requested, **kwargs):
        until(cancel_requested)
        return result_for(task)

    service = service_factory(execute)
    web = SimpleNamespace(benchmark_service=service, benchmark_lock=Lock(), model_config=lambda: None)
    with TestClient(create_benchmark_app(web)) as client:
        response = client.post("/run", json={"task": ALL_TASKS[0].name})
        assert response.status_code == 202
        batch = response.json()
        assert batch["finished"] == 0
        assert client.get("/runs").json()["instance_id"] == batch["instance_id"]
        assert client.post("/run-all", json={}).status_code == 409
        missing = client.get("/runs/missing")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "运行记录不存在或已失效。"
        assert client.post("/runs/missing/cancel").status_code == 404
        assert client.post(f"/runs/{batch['id']}/cancel", json={}).status_code == 202
        until(lambda: client.get(f"/runs/{batch['id']}").json()["status"] in FINISHED)
        assert client.post("/run", json={"task": "missing"}).status_code == 404
        assert client.post("/run-all", json={"planner": "missing"}).status_code == 422


def test_instance_changes_after_restart(service_factory):
    first = service_factory(lambda task, **kwargs: result_for(task))
    second = service_factory(lambda task, **kwargs: result_for(task))
    assert first.instance_id != second.instance_id
    assert second.snapshot()["runs"] == []
