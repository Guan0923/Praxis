"""Task-scoped, explicitly prepared benchmark resources and ownership."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Lock, RLock
from time import monotonic
from uuid import uuid4

from backend.configuration import atomic_write_text
from backend.domain import safe_error_message
from backend.jobs import TERMINAL_STATES, AdmissionPolicy, JobLane, QueueMode, SlotMode, ThreadJob

from .containers import ContainerCancelled, TaskContainer, cache_root, command
from .import_public_suite import DATA_REV, SWE_REV, TB_REV, check_revision
from .model import BenchmarkTask

BUSY = {"preparing", "deleting"}
_stores: dict[Path, ResourceStore] = {}
_stores_lock = Lock()


class ResourceConflict(ValueError):
    pass


def resource_store(cache: Path | None = None) -> ResourceStore:
    root = (cache or cache_root()).absolute()
    with _stores_lock:
        if root not in _stores:
            _stores[root] = ResourceStore(root)
        return _stores[root]


class ResourceStore:
    def __init__(self, cache: Path) -> None:
        self.cache = cache.absolute()
        self.lock = RLock()
        self._locks: dict[str, Lock] = {}
        self._runs: dict[str, int] = {}
        self._operations: dict[str, str] = {}
        self._checked = 0.0
        self.path = self._path("resources.json")
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "id": uuid4().hex,
                "tasks": {},
                "resources": {},
            }
        )
        for value in self.data["tasks"].values():
            if value["status"] in BUSY:
                value.update(
                    status="error",
                    phase="interrupted",
                    error="Resource operation interrupted. Retry download or deletion.",
                )

    def _path(self, relative: str) -> Path:
        target = self.cache / relative
        if not target.absolute().is_relative_to(self.cache) or ".." in Path(relative).parts:
            raise RuntimeError("Resource path is outside the benchmark cache.")
        for path in (target, *target.parents):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise RuntimeError(f"Refusing a linked benchmark resource path: {path}")
        return target

    def _save(self) -> None:
        self._path("resources.json")
        self.cache.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(self.data, ensure_ascii=True, indent=2))

    def _status(self, task: BenchmarkTask, status: str, phase: str, error: str | None = None) -> None:
        with self.lock:
            self.data["tasks"][task.name] = {"status": status, "phase": phase, "error": error}
            self._save()

    def _claim(self, task: BenchmarkTask, kind: str, target: str, *, owned: bool) -> None:
        with self.lock:
            key = f"{kind}:{target}"
            entry = self.data["resources"].setdefault(
                key, {"kind": kind, "target": target, "owned": owned, "users": []}
            )
            if task.name not in entry["users"]:
                entry["users"].append(task.name)
            self._save()

    def _claim_path(self, task: BenchmarkTask, relative: str) -> Path:
        path = self._path(relative)
        self._claim(task, "path", relative, owned=not path.exists())
        return path

    def _resource_lock(self, key: str) -> Lock:
        with self.lock:
            return self._locks.setdefault(key, Lock())

    def _writable_path(self, task: BenchmarkTask, relative: str) -> Path:
        path = self._claim_path(task, relative)
        with self.lock:
            if not self.data["resources"][f"path:{relative}"]["owned"]:
                raise RuntimeError(f"Refusing to overwrite an unmanaged resource: {path}")
        return path

    @staticmethod
    def _acquire(lock: Lock, cancelled: Callable[[], bool]) -> None:
        while not lock.acquire(timeout=0.1):
            if cancelled():
                raise ContainerCancelled("Resource preparation stopped.")
        if cancelled():
            lock.release()
            raise ContainerCancelled("Resource preparation stopped.")

    def _remove_path(self, relative: str) -> None:
        path = self._path(relative)
        if path == self.cache or not path.resolve().is_relative_to(self.cache.resolve()):
            raise RuntimeError("Refusing to remove the benchmark cache root or an external path.")
        if not path.exists():
            return

        def writable_retry(function, name, _exc):
            child = Path(name)
            if not child.resolve().is_relative_to(path.resolve()):
                raise RuntimeError("Refusing an external deletion target.")
            child.chmod(stat.S_IWRITE | stat.S_IREAD)
            function(name)

        if path.is_dir():
            shutil.rmtree(path, onerror=writable_retry)
        else:
            path.unlink()

    def _source(self, task: BenchmarkTask, cancelled: Callable[[], bool]) -> None:
        terminal = task.container["kind"] == "terminal_bench"
        name, url, revision = (
            ("terminal-bench-2", "https://github.com/harbor-framework/terminal-bench-2.git", TB_REV)
            if terminal
            else ("swe-bench-pro", "https://github.com/scaleapi/SWE-bench_Pro-os.git", SWE_REV)
        )
        lock = self._resource_lock(name)
        self._acquire(lock, cancelled)
        try:
            root = self._claim_path(task, name)
            if not root.exists():
                temporary = self._claim_path(task, name + ".download")
                if temporary.exists():
                    with self.lock:
                        owned = self.data["resources"][f"path:{name}.download"]["owned"]
                    if not owned:
                        raise RuntimeError("An unmanaged download directory already exists.")
                    self._remove_path(name + ".download")
                command(["git", "init", str(temporary)], cancelled=cancelled)
                git = ["git", "-C", str(temporary), "-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
                command([*git, "fetch", "--depth", "1", url, revision], timeout=900, cancelled=cancelled)
                command([*git, "checkout", "--detach", revision], timeout=120, cancelled=cancelled)
                check_revision(temporary, revision)
                temporary.rename(root)
            check_revision(root, revision)
        finally:
            lock.release()

    def _swe_data(self, task: BenchmarkTask, cancelled: Callable[[], bool]) -> None:
        lock = self._resource_lock("swe-data")
        self._acquire(lock, cancelled)
        try:
            environment = self._claim_path(task, "swe-data-env")
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.exists():
                self._writable_path(task, "swe-data-env")
                command([sys.executable, "-m", "venv", str(environment)], timeout=120, cancelled=cancelled)
            code, _ = command(
                [str(python), "-c", "import pyarrow,sys; sys.exit(pyarrow.__version__ != '25.0.1')"],
                check=False,
                cancelled=cancelled,
            )
            if code:
                self._writable_path(task, "swe-data-env")
                command(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--only-binary=:all:",
                        "--force-reinstall",
                        "pyarrow==25.0.1",
                    ],
                    timeout=600,
                    cancelled=cancelled,
                )
            parquet_name = f"swe-pro-{DATA_REV}.parquet"
            parquet = self._claim_path(task, parquet_name)
            if not parquet.exists():
                temporary = self._writable_path(task, parquet_name + ".download")
                url = f"https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/{DATA_REV}/data/test-00000-of-00001.parquet"
                with urllib.request.urlopen(url, timeout=30) as response, temporary.open("wb") as output:
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        if cancelled():
                            raise ContainerCancelled("Resource download stopped.")
                        size += len(chunk)
                        if size > 50_000_000:
                            raise RuntimeError("Unexpectedly large benchmark dataset.")
                        output.write(chunk)
                command(
                    [
                        str(python),
                        "-c",
                        "import pyarrow.parquet as p,sys; p.read_metadata(sys.argv[1])",
                        str(temporary),
                    ],
                    timeout=120,
                    cancelled=cancelled,
                )
                temporary.rename(parquet)
            row = self._claim_path(task, f"swe-rows/{task.name}.json")
            if not row.exists():
                row.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._writable_path(task, f"swe-rows/{task.name}.json.download")
                command(
                    [
                        str(python),
                        str(Path(__file__).with_name("read_swe_data.py")),
                        str(parquet),
                        task.source.task_id,
                        str(temporary),
                    ],
                    timeout=120,
                    cancelled=cancelled,
                )
                json.loads(temporary.read_text(encoding="utf-8"))[task.source.task_id]
                temporary.rename(row)
        finally:
            lock.release()

    def prepare(self, task: BenchmarkTask, cancelled: Callable[[], bool] = lambda: False) -> None:
        if task.container is None:
            self._status(task, "ready", "ready")
            return
        from .prepare import environment_version, prepare_task

        self._status(task, "preparing", "sources")
        self._source(task, cancelled)
        if task.container["kind"] == "swe_bench_pro":
            self._status(task, "preparing", "data")
            self._swe_data(task, cancelled)
        receipt_name = f"prepared/{task.name}.json"
        receipt = self._claim_path(task, receipt_name)
        if receipt.exists():
            try:
                previous = json.loads(receipt.read_text(encoding="utf-8"))
                reusable = (
                    previous.get("prepared")
                    and previous.get("suite_version") == task.suite_version
                    and previous.get("environment_version") == environment_version(task)
                    and previous.get("source_image_id") == task.container["source_image_id"]
                    and previous.get("image_id")
                )
                if reusable:
                    reusable = command(["docker", "image", "inspect", previous["image_id"]], check=False)[0] == 0
            except (ValueError, AttributeError):
                reusable = False
            if not reusable:
                self._writable_path(task, receipt_name)
                self._remove_path(receipt_name)
        self._status(task, "preparing", "image")
        image = task.container["image"]
        lock = self._resource_lock("image:" + image)
        self._acquire(lock, cancelled)
        try:
            container = TaskContainer(task, cancelled, cache=self.cache)
            code, _ = container.docker("image", "inspect", image, check=False)
            self._claim(task, "image", image, owned=code != 0)
            container.prepare_image(prepared=False)
        finally:
            lock.release()
        self._status(task, "preparing", "environment")
        tag = "praxis-benchmark:" + self.data["id"] + "-" + task.name
        if task.container["kind"] == "terminal_bench":
            self._claim(task, "image", tag, owned=True)
        prepare_task(task, self.cache, cancelled, image_tag=tag)
        self._check(task, self._images(cancelled), {})
        self._status(task, "ready", "ready")

    def delete(self, task: BenchmarkTask, cancelled: Callable[[], bool] = lambda: False) -> None:
        failures = []
        self._status(task, "deleting", "cleanup")
        # The task remains reserved until every deletion completes. Shared claims
        # are checked while locked so a new download cannot race last-user removal.
        with self.lock:
            entries = [(key, value) for key, value in self.data["resources"].items() if task.name in value["users"]]
            for key, entry in reversed(entries):
                if cancelled():
                    raise ContainerCancelled("Resource deletion stopped.")
                try:
                    if len(entry["users"]) == 1 and entry["owned"]:
                        if entry["kind"] == "path":
                            self._remove_path(entry["target"])
                        else:
                            command(["docker", "image", "ls", "--quiet"], timeout=15)
                            code, _ = command(["docker", "image", "inspect", entry["target"]], check=False)
                            if code == 0:
                                _, containers = command(
                                    ["docker", "ps", "-aq", "--filter", "ancestor=" + entry["target"]]
                                )
                                if containers.strip():
                                    raise RuntimeError("Image is still used by a Docker container.")
                                command(["docker", "image", "rm", entry["target"]], timeout=120)
                    entry["users"].remove(task.name)
                    if not entry["users"]:
                        del self.data["resources"][key]
                    self._save()
                except Exception as exc:
                    failures.append(f"{entry['target']}: {safe_error_message(exc)}")
        if failures:
            raise RuntimeError("Some resources remain: " + "; ".join(failures))
        self._status(task, "not_prepared", "idle")

    @staticmethod
    def _images(cancelled=None) -> dict[str, str]:
        _, output = command(
            ["docker", "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
            timeout=15,
            cancelled=cancelled,
        )
        return dict(line.split() for line in output.splitlines() if line.strip())

    def _check(self, task: BenchmarkTask, images: dict[str, str], repos: dict[str, str | None]) -> None:
        from .prepare import environment_version

        terminal = task.container["kind"] == "terminal_bench"
        name, revision = ("terminal-bench-2", TB_REV) if terminal else ("swe-bench-pro", SWE_REV)
        if name not in repos:
            try:
                check_revision(self._path(name), revision)
                repos[name] = None
            except Exception as exc:
                repos[name] = safe_error_message(exc)
        if repos[name]:
            raise RuntimeError(repos[name])
        receipt = json.loads(self._path(f"prepared/{task.name}.json").read_text(encoding="utf-8"))
        if (
            not receipt.get("prepared")
            or receipt.get("suite_version") != task.suite_version
            or receipt.get("environment_version") != environment_version(task)
        ):
            raise RuntimeError("Task resources are not prepared for this suite version.")
        if (
            receipt.get("image_id") not in images.values()
            or images.get(task.container["image"]) != task.container["source_image_id"]
            or receipt.get("source_image_id") != task.container["source_image_id"]
        ):
            raise RuntimeError("A required Docker image is missing. Download task resources again.")
        source = self._path(name) / (task.container["task_path"] if terminal else "run_scripts/" + task.source.task_id)
        if not source.is_dir():
            raise RuntimeError("Task source files are missing.")
        if not terminal:
            json.loads(self._path(f"swe-rows/{task.name}.json").read_text(encoding="utf-8"))[task.source.task_id]
            python = self._path("swe-data-env") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.is_file() or not self._path(f"swe-pro-{DATA_REV}.parquet").is_file():
                raise RuntimeError("SWE data or its isolated environment is missing. Download task resources again.")

    def snapshot(self, tasks: Sequence[BenchmarkTask], *, refresh: bool = False) -> list[dict]:
        with self.lock:
            if refresh or monotonic() - self._checked > 5:
                ready = [
                    task
                    for task in tasks
                    if task.container and self.data["tasks"].get(task.name, {}).get("status") == "ready"
                ]
                images, repos = {}, {}
                if ready:
                    try:
                        images = self._images()
                    except Exception:
                        images = {}
                for task in ready:
                    try:
                        self._check(task, images, repos)
                    except Exception as exc:
                        self._status(task, "error", "validation", safe_error_message(exc))
                self._checked = monotonic()
            return [
                {
                    "task_name": task.name,
                    **self.data["tasks"].get(
                        task.name,
                        {"status": "not_prepared" if task.container else "ready", "phase": "idle", "error": None},
                    ),
                    "has_resources": any(task.name in value["users"] for value in self.data["resources"].values()),
                    "in_use": self._runs.get(task.name, 0) > 0,
                }
                for task in tasks
            ]

    def ensure_ready(self, tasks: Sequence[BenchmarkTask]) -> None:
        states = self.snapshot(tasks, refresh=True)
        missing = [value for value in states if value["status"] != "ready"]
        if missing:
            details = "; ".join(
                value["task_name"] + (": " + value["error"] if value["error"] else "") for value in missing
            )
            raise ResourceConflict("Download resources before running: " + details)

    def reserve(self, tasks: Sequence[BenchmarkTask]) -> None:
        with self.lock:
            self.ensure_ready(tasks)
            for task in tasks:
                self._runs[task.name] = self._runs.get(task.name, 0) + 1

    def release(self, task: BenchmarkTask) -> None:
        with self.lock:
            self._runs[task.name] = max(0, self._runs.get(task.name, 0) - 1)

    def submit(self, task: BenchmarkTask, action: str, scope) -> dict:
        with self.lock:
            state = self.data["tasks"].get(task.name, {})
            if self._runs.get(task.name, 0):
                raise ResourceConflict("Task is queued or running; its resources are in use.")
            status = "preparing" if action == "prepare" else "deleting"
            if state.get("status") in BUSY:
                if state["status"] == status:
                    return self.snapshot([task])[0]
                raise ResourceConflict("Another resource operation is already in progress.")
            self._status(task, status, "queued")

            def worker(*, is_cancelled):
                try:
                    if action == "prepare":
                        self.prepare(task, is_cancelled)
                    else:
                        self.delete(task, is_cancelled)
                except Exception as exc:
                    with self.lock:
                        phase = self.data["tasks"][task.name]["phase"]
                        self._status(task, "error", phase, safe_error_message(exc))

            job = ThreadJob(scope.registry.new_job_id(), worker)
            self._operations[task.name] = job.info().id
            store = self

            class Completion:
                def on_job_state_change(self, change):
                    if change.job_info.state in TERMINAL_STATES:
                        with store.lock:
                            if store._operations.get(task.name) != change.job_info.id:
                                return
                            store._operations.pop(task.name, None)
                            if store.data["tasks"][task.name]["status"] in BUSY:
                                store._status(
                                    task,
                                    "error",
                                    "interrupted",
                                    "Resource operation stopped before completion. Retry the operation.",
                                )

            job.add_listener(Completion())
            try:
                scope.submit(
                    job,
                    lane=JobLane.BACKGROUND,
                    admission=AdmissionPolicy(queue_mode=QueueMode.WAIT, slot_mode=SlotMode.COUNTED),
                )
            except Exception as exc:
                self._operations.pop(task.name, None)
                self._status(task, "error", action, safe_error_message(exc))
                raise
            return self.snapshot([task])[0]
