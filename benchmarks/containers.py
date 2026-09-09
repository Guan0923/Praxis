"""Benchmark-only Docker execution. No host mounts, credentials or Docker socket."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from time import monotonic
from uuid import uuid4

from backend.tools.base import Tool, ToolError, ToolInvocationContext
from backend.tools.registry import ToolRegistry

from .model import BenchmarkTask

_image_locks: dict[str, Lock] = {}
_locks_guard = Lock()


class ContainerCancelled(RuntimeError):
    pass


class ContainerTimeout(TimeoutError):
    pass


def cache_root() -> Path:
    return Path(os.environ.get("MINI_AGENT_BENCHMARK_CACHE", str(Path.home() / ".cache" / "mini-agent-benchmark")))


def command(
    args: list[str],
    *,
    timeout: float = 60,
    cancelled: Callable[[], bool] | None = None,
    data: bytes | None = None,
    check: bool = True,
    limit: int = 2_000_000,
) -> tuple[int, str]:
    """Drain output to disk, not an unbounded PIPE; poll cancellation while waiting."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as source:
        if data is not None:
            source.write(data)
            source.seek(0)
        process = subprocess.Popen(
            args,
            stdin=source if data is not None else subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled():
                    raise ContainerCancelled("Benchmark stopped.")
                if monotonic() >= deadline:
                    raise ContainerTimeout("Benchmark operation timed out.")
                if output.tell() > 16_000_000:
                    raise RuntimeError("Benchmark command exceeded its output limit.")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        size = output.tell()
        output.seek(0)
        text = output.read(limit).decode("utf-8", errors="replace")
        if size > limit:
            text += "\n[output truncated]"
        if check and process.returncode:
            # Do not include command arguments: they may contain user content.
            raise RuntimeError(f"Benchmark command failed ({process.returncode}): {text[-4000:]}")
        return process.returncode, text


def image_status(task: BenchmarkTask) -> str:
    if task.container is None:
        return "local"
    receipt = cache_root() / "prepared" / f"{task.name}.json"
    if not receipt.exists():
        return "not_prepared"
    try:
        value = json.loads(receipt.read_text(encoding="utf-8"))
        if value.get("suite_version") != task.suite_version or not value.get("image_id"):
            return "not_prepared"
        return "verified" if value.get("verified") else "downloaded"
    except (ValueError, OSError):
        return "not_prepared"


class TaskContainer:
    def __init__(self, task: BenchmarkTask, cancelled: Callable[[], bool] | None = None, *, cache: Path | None = None):
        if task.container is None:
            raise ValueError("Task has no container specification.")
        self.task = task
        self.spec = task.container
        self.cache = cache or cache_root()
        self.cancelled = cancelled
        self.name = "mini-agent-bench-" + uuid4().hex
        self.image = ""
        self.workdir = "/app"
        self.started = False
        self.tools_open = True
        self.build_proxy: str | None = None

    def docker(self, *args: str, timeout: float = 60, data: bytes | None = None, check: bool = True) -> tuple[int, str]:
        return command(["docker", *args], timeout=timeout, cancelled=self.cancelled, data=data, check=check)

    def prepare_image(self, *, prepared: bool = True) -> str:
        image = self.spec["image"]
        with _locks_guard:
            lock = _image_locks.setdefault(image, Lock())
        while not lock.acquire(timeout=0.1):
            if self.cancelled and self.cancelled():
                raise ContainerCancelled("Benchmark stopped during environment preparation.")
        try:
            receipt = self.cache / "prepared" / f"{self.task.name}.json"
            pinned = None
            if receipt.exists():
                previous = json.loads(receipt.read_text(encoding="utf-8"))
                if previous.get("suite_version") == self.task.suite_version:
                    pinned = previous.get("image_id")
            code, value = self.docker("image", "inspect", image, "--format", "{{.Id}}", check=False)
            if code:
                self.docker("pull", "--platform", "linux/amd64", image, timeout=self.spec["build_seconds"])
                _, value = self.docker("image", "inspect", image, "--format", "{{.Id}}")
            self.image = value.strip()
            if self.spec.get("source_image_id") and self.image != self.spec["source_image_id"]:
                raise RuntimeError("Source image differs from the fixed suite image; refusing an environment update.")
            source_id = previous.get("source_image_id") if receipt.exists() and pinned else None
            if pinned and (source_id or pinned) != self.image:
                raise RuntimeError(
                    "Cached image differs from the accepted image; refusing an implicit environment update."
                )
            if pinned and source_id and prepared:
                code, _ = self.docker("image", "inspect", pinned, check=False)
                if code:
                    raise RuntimeError("Prepared benchmark image is missing; prepare the task environment again.")
                self.image = pinned
            return self.image
        finally:
            lock.release()

    def start(self, *, image: str | None = None, network: bool = False) -> None:
        if image is None:
            image = self.prepare_image()
        self.image = image
        if network:
            proxy = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
            if proxy:
                parsed = urllib.parse.urlsplit(proxy)
                if parsed.hostname in {"localhost", "127.0.0.1"}:
                    proxy = proxy.replace(parsed.hostname, "host.docker.internal")
                self.build_proxy = proxy
        _, config_text = self.docker("image", "inspect", image, "--format", "{{json .Config}}")
        config = json.loads(config_text)
        self.workdir = config.get("WorkingDir") or "/app"
        # Mark ownership before creation so cancellation cannot orphan a container.
        self.started = True
        try:
            proxy_environment = [
                part
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                )
                for part in ("--env", key + "=")
            ]
            self.docker(
                "run",
                "-d",
                *proxy_environment,
                "--env",
                "UV_OFFLINE=" + ("0" if network else "1"),
                "--name",
                self.name,
                "--label",
                "mini-agent.benchmark=true",
                "--network",
                "bridge" if network else "none",
                "--memory",
                f"{self.spec['memory_mb']}m",
                "--cpus",
                str(self.spec["cpus"]),
                "--pids-limit",
                "512",
                "--security-opt",
                "no-new-privileges=true",
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                "while :; do sleep 3600; done",
            )
        except BaseException:
            self.close()
            raise

    def exec(
        self, script: str, *, timeout: float = 60, data: bytes | None = None, check: bool = True
    ) -> tuple[int, str]:
        try:
            environment = (
                [
                    part
                    for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY")
                    for part in ("--env", f"{key}={self.build_proxy}")
                ]
                if self.build_proxy
                else []
            )
            return self.docker(
                "exec",
                "-i",
                "-w",
                self.workdir,
                *environment,
                self.name,
                "/bin/bash",
                "-c",
                script,
                timeout=timeout,
                data=data,
                check=check,
            )
        except (ContainerCancelled, ContainerTimeout):
            self.close()
            raise

    def upload(self, source: Path, destination: str) -> None:
        self.docker("cp", str(source), f"{self.name}:{destination}", timeout=120)

    def write(self, path: str, content: str) -> None:
        self.exec("cat > " + shlex.quote(path), data=content.encode("utf-8"))

    def close(self) -> None:
        self.tools_open = False
        if self.started:
            # Exact generated name only; never enumerate or prune other containers.
            code, text = command(["docker", "rm", "-f", self.name], timeout=30, check=False)
            if code and "No such container" not in text:
                raise RuntimeError("Could not remove benchmark container: " + text[-1000:])
            self.started = False

    def tools(self) -> ToolRegistry:
        def execute(context: ToolInvocationContext, command: str, timeout_seconds: int = 60) -> str:
            if not self.tools_open:
                raise ToolError("The benchmark execution has ended.")
            if context.cancel_requested and context.cancel_requested():
                raise ContainerCancelled("Benchmark stopped.")
            code, text = self.exec(command, timeout=timeout_seconds, check=False)
            return f"exit_code={code}\n{text[:50000]}"

        def read(context: ToolInvocationContext, path: str) -> str:
            return execute(context, "head -c 50000 -- " + shlex.quote(path))

        def write(context: ToolInvocationContext, path: str, content: str) -> str:
            if not self.tools_open or (context.cancel_requested and context.cancel_requested()):
                raise ContainerCancelled("Benchmark stopped.")
            self.write(path, content)
            return "File written in the task container."

        def schema(properties: dict, required: list[str]) -> dict:
            return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

        text = {"type": "string"}
        return ToolRegistry(
            [
                Tool(
                    "container_exec",
                    f"Run a Bash command inside the isolated task container. Working directory: {self.workdir}. No host access.",
                    lambda **kwargs: execute(ToolInvocationContext(), **kwargs),
                    schema(
                        {"command": text, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
                        ["command"],
                    ),
                    requires_confirmation=True,
                    read_only=False,
                    context_handler=execute,
                ),
                Tool(
                    "container_read_file",
                    "Read a file inside the task container (at most 50000 bytes).",
                    lambda **kwargs: read(ToolInvocationContext(), **kwargs),
                    schema({"path": text}, ["path"]),
                    context_handler=read,
                ),
                Tool(
                    "container_write_file",
                    "Write UTF-8 text to a file inside the task container.",
                    lambda **kwargs: write(ToolInvocationContext(), **kwargs),
                    schema({"path": text, "content": {"type": "string", "maxLength": 1000000}}, ["path", "content"]),
                    requires_confirmation=True,
                    read_only=False,
                    context_handler=write,
                ),
            ]
        )
