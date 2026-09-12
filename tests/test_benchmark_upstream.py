"""Upstream transfer checks using real Git archives, including Windows settings."""

from __future__ import annotations

import io
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from benchmarks import upstream


@pytest.mark.parametrize("kind", ["terminal_bench", "swe_bench_pro"])
@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_upload_preserves_upstream_bytes_with_windows_git_settings(tmp_path, monkeypatch, kind, autocrlf):
    terminal = kind == "terminal_bench"
    root = tmp_path / ("terminal-bench-2" if terminal else "swe-bench-pro")
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=30).stdout

    git("init", "-q")
    source = root / ("task/tests" if terminal else "run_scripts/task")
    source.mkdir(parents=True)
    (source / ".gitattributes").write_bytes(b"* text=auto\n")
    script = b"#!/bin/bash\nprintf 'upstream script\\n'\n"
    binary = b"\x00\r\n\xff\n"
    (source / "test.sh").write_bytes(script)
    (source / "fixture.bin").write_bytes(binary)
    git("-c", "core.autocrlf=false", "-c", "core.eol=lf", "add", ".")
    git("-c", "user.name=Benchmark Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture")
    revision = git("rev-parse", "HEAD").decode().strip()
    monkeypatch.setattr(upstream, "TB_REV" if terminal else "SWE_REV", revision)
    git("config", "core.autocrlf", autocrlf)
    git("config", "core.eol", "crlf")
    (source / "test.sh").write_bytes(b"uncommitted checkout changes\r\n")
    transfers = []
    container = SimpleNamespace(
        cache=tmp_path,
        spec={"kind": kind, "task_path": "task"},
        task=SimpleNamespace(source=SimpleNamespace(task_id="task")),
        exec=lambda command, *, data: transfers.append(data),
    )

    upstream.upload_upstream(container, "tests" if terminal else "", "/tests")

    with tarfile.open(fileobj=io.BytesIO(transfers[0])) as archive:
        assert archive.extractfile("test.sh").read() == script
        assert archive.extractfile("fixture.bin").read() == binary
