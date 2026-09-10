from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from backend.jobs import JobRegistry
from benchmarks.import_public_suite import check_revision
from benchmarks.resources import ResourceConflict, ResourceStore
from benchmarks.service import BenchmarkService
from benchmarks.tasks import ALL_TASKS, TASKS_BY_NAME
from tests.test_benchmark_service import until


def test_missing_and_non_git_sources_have_actionable_errors(tmp_path):
    with pytest.raises(RuntimeError, match="Download task resources"):
        check_revision(tmp_path / "missing", "revision")
    with pytest.raises(RuntimeError, match="not a Git repository"):
        check_revision(tmp_path, "revision")


def test_no_resources_blocks_single_and_batch_before_execution(tmp_path):
    calls = []
    registry = JobRegistry()
    service = BenchmarkService(
        registry,
        tmp_path / "runs",
        resources=ResourceStore(tmp_path / "cache"),
        execute=lambda *a, **k: calls.append(a),
    )
    try:
        for tasks in ([ALL_TASKS[0]], ALL_TASKS):
            with pytest.raises(ResourceConflict, match="Download resources"):
                service.start(tasks, "llm", None)
        assert calls == [] and service.snapshot()["runs"] == []
    finally:
        service.close()
        registry.close_all(timeout=5)


def test_shared_and_unmanaged_files_survive_until_last_owner(tmp_path):
    store = ResourceStore(tmp_path)
    first, second = ALL_TASKS[:2]
    shared = store._claim_path(first, "shared/data.txt")
    shared.parent.mkdir()
    shared.write_text("shared")
    store._claim_path(second, "shared/data.txt")
    own = store._claim_path(first, "own.txt")
    own.write_text("first")
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("user data")
    store._claim_path(first, "foreign.txt")
    store.delete(first)
    assert shared.exists() and foreign.exists() and not own.exists()
    store.delete(second)
    assert not shared.exists() and foreign.read_text() == "user data"


def test_partial_delete_can_retry_without_forgetting_remaining_files(tmp_path, monkeypatch):
    store = ResourceStore(tmp_path)
    task = ALL_TASKS[0]
    path = store._claim_path(task, "blocked.txt")
    path.write_text("keep")
    remove = store._remove_path
    monkeypatch.setattr(store, "_remove_path", lambda _: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(RuntimeError, match="Some resources remain"):
        store.delete(task)
    assert store.snapshot([task])[0]["has_resources"]
    monkeypatch.setattr(store, "_remove_path", remove)
    store.delete(task)
    assert not path.exists()


def test_download_is_deduplicated_and_running_task_cannot_delete(tmp_path, monkeypatch):
    store = ResourceStore(tmp_path / "cache")
    entered, release = Event(), Event()
    task = ALL_TASKS[0]
    calls = []

    def prepare(task, cancelled):
        calls.append(task.name)
        entered.set()
        release.wait(5)
        store._status(task, "ready", "ready")

    monkeypatch.setattr(store, "prepare", prepare)
    registry = JobRegistry()
    service = BenchmarkService(registry, tmp_path / "runs", resources=store)
    try:
        service.resource_operation(task, "prepare")
        assert entered.wait(3)
        service.resource_operation(task, "prepare")
        with pytest.raises(ResourceConflict):
            service.resource_operation(task, "delete")
        assert len(calls) == 1
        store._runs[task.name] = 1
        with pytest.raises(ResourceConflict, match="in use"):
            service.resource_operation(task, "delete")
    finally:
        release.set()
        service.close()
        registry.close_all(timeout=5)


def test_restart_does_not_trust_incomplete_operations_or_missing_images(tmp_path):
    store = ResourceStore(tmp_path)
    store._status(ALL_TASKS[0], "preparing", "sources")
    reopened = ResourceStore(tmp_path)
    assert reopened.snapshot([ALL_TASKS[0]])[0]["status"] == "error"


def test_unmanaged_resource_cannot_be_overwritten(tmp_path):
    store = ResourceStore(tmp_path)
    existing = tmp_path / "partial.download"
    existing.write_text("user data")
    with pytest.raises(RuntimeError, match="unmanaged"):
        store._writable_path(ALL_TASKS[0], existing.name)
    assert existing.read_text() == "user data"


def test_ready_state_rechecks_source_image_tag(tmp_path, monkeypatch):
    import benchmarks.resources as resources
    from benchmarks.prepare import environment_version

    task = ALL_TASKS[0]
    store = ResourceStore(tmp_path)
    receipt = store._claim_path(task, f"prepared/{task.name}.json")
    receipt.parent.mkdir()
    receipt.write_text(
        json.dumps(
            {
                "prepared": True,
                "suite_version": task.suite_version,
                "environment_version": environment_version(task),
                "image_id": "prepared-image",
                "source_image_id": task.container["source_image_id"],
            }
        )
    )
    monkeypatch.setattr(resources, "check_revision", lambda *args: None)
    monkeypatch.setattr(store, "_images", lambda: {"another-tag": "prepared-image"})
    store._status(task, "ready", "ready")
    state = store.snapshot([task], refresh=True)[0]
    assert state["status"] == "error" and "Docker image" in state["error"]


def test_real_git_shared_download_and_retry(tmp_path, monkeypatch):
    import benchmarks.resources as resources

    original = resources.command
    source = tmp_path / "upstream"
    source.mkdir()
    (source / "fixture.txt").write_text("local source")
    original(["git", "init", str(source)])
    original(["git", "-C", str(source), "add", "."])
    original(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Resource test",
            "-c",
            "user.email=resource@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "fixture",
        ]
    )
    _, revision = original(["git", "-C", str(source), "rev-parse", "HEAD"])
    monkeypatch.setattr(resources, "TB_REV", revision.strip())
    fetches = []

    def local_command(args, **kwargs):
        if "fetch" in args:
            fetches.append(args)
            if len(fetches) == 1:
                raise RuntimeError("test connection interrupted")
            args = [str(source) if item.startswith("https://github.com/") else item for item in args]
        return original(args, **kwargs)

    monkeypatch.setattr(resources, "command", local_command)
    store = ResourceStore(tmp_path / "cache")
    first, second = ALL_TASKS[:2]
    with pytest.raises(RuntimeError, match="interrupted"):
        store._source(first, lambda: False)
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(lambda task: store._source(task, lambda: False), [first, second]))
    assert len(fetches) == 2
    assert (store.cache / "terminal-bench-2" / "fixture.txt").read_text() == "local source"
    store.delete(first)
    assert (store.cache / "terminal-bench-2").exists()
    store.delete(second)
    assert not (store.cache / "terminal-bench-2").exists()
    store._source(first, lambda: False)
    assert len(fetches) == 3
    assert (store.cache / "terminal-bench-2" / "fixture.txt").read_text() == "local source"


def test_real_http_resources_do_not_require_a_model(tmp_path, monkeypatch):
    import socket
    from threading import Lock, Thread
    from types import SimpleNamespace

    import httpx
    import uvicorn

    from backend.api.shared.benchmark import create_benchmark_app

    store = ResourceStore(tmp_path / "cache")
    registry = JobRegistry()
    service = BenchmarkService(registry, tmp_path / "runs", resources=store)
    entered, release = Event(), Event()

    def prepare(task, cancelled):
        entered.set()
        release.wait(5)
        path = store._claim_path(task, "test-resource.txt")
        path.write_text("real HTTP worker")
        store._status(task, "error", "image", "intentional local download failure")

    monkeypatch.setattr(store, "prepare", prepare)

    def no_model():
        raise RuntimeError("No model is configured")

    web = SimpleNamespace(benchmark_service=service, benchmark_lock=Lock(), model_config=no_model)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_benchmark_app(web), log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        until(lambda: server.started)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            task = ALL_TASKS[0]
            endpoint = f"/tasks/{task.name}/resources"
            assert client.post("/run", json={"task": task.name}).status_code == 409
            assert client.post("/run-all", json={}).status_code == 409
            assert client.post(endpoint).status_code == 202
            assert entered.wait(3)
            assert client.post(endpoint).status_code == 202
            assert client.delete(endpoint).status_code == 409
            release.set()
            until(lambda: client.get("/resources").json()[0]["status"] == "error")
            assert client.delete(endpoint).status_code == 202
            until(lambda: client.get("/resources").json()[0]["status"] == "not_prepared")
            assert not (store.cache / "test-resource.txt").exists()
    finally:
        release.set()
        server.should_exit = True
        thread.join(10)
        listener.close()
        service.close()
        registry.close_all(timeout=5)


@pytest.mark.skipif(
    os.environ.get("PRAXIS_RESOURCE_LIVE") != "1", reason="Explicit public download and Docker acceptance"
)
@pytest.mark.parametrize(
    "name", ["tb2-log-summary-date-ranges", "swepro-ansible__ansible-a26c325bd8f6e2822d9d7e62f77a424c1db4fbf6"]
)
def test_real_public_resources(name, tmp_path):
    from benchmarks.containers import TaskContainer
    from benchmarks.upstream import prepare_environment

    task = TASKS_BY_NAME[name]
    store = ResourceStore(tmp_path / "public-cache")
    store.prepare(task)
    assert store.snapshot([task], refresh=True)[0]["status"] == "ready"
    receipt_path = store.cache / "prepared" / f"{task.name}.json"
    receipt = json.loads(receipt_path.read_text())
    store.prepare(task)
    assert json.loads(receipt_path.read_text())["image_id"] == receipt["image_id"]
    container = TaskContainer(task, cache=store.cache)
    try:
        prepare_environment(container)
        assert container.exec("printf RESOURCE_READY")[1] == "RESOURCE_READY"
    finally:
        container.close()


@pytest.mark.skipif(os.environ.get("PRAXIS_RESOURCE_LIVE") != "1", reason="Explicit local Docker acceptance")
def test_real_docker_shared_deletion_and_container_protection(tmp_path):
    from uuid import uuid4

    from benchmarks.containers import command

    first, second = ALL_TASKS[:2]
    source = TASKS_BY_NAME["tb2-log-summary-date-ranges"].container["image"]
    tag = "praxis-benchmark:delete-test-" + uuid4().hex
    name = "praxis-resource-delete-test-" + uuid4().hex
    store = ResourceStore(tmp_path / "cache")
    store._claim(first, "image", tag, owned=True)
    store._claim(second, "image", tag, owned=True)
    command(["docker", "tag", source, tag])
    created = False
    try:
        store.delete(first)
        command(["docker", "image", "inspect", tag])
        command(["docker", "create", "--name", name, "--entrypoint", "/bin/true", tag])
        created = True
        with pytest.raises(RuntimeError, match="still used"):
            store.delete(second)
        assert store.snapshot([second])[0]["has_resources"]
        command(["docker", "rm", name])
        created = False
        store.delete(second)
        assert command(["docker", "image", "inspect", tag], check=False)[0] != 0
        command(["docker", "image", "inspect", source])
    finally:
        if created:
            command(["docker", "rm", name], check=False)
        command(["docker", "image", "rm", tag], check=False)
