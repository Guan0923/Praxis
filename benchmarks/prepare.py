"""Download pinned source data, prepare images, and verify without a model API.

uv run --with pyarrow==25.0.1 python -m benchmarks.prepare --sources
python -m benchmarks.prepare --verify --task tb2-log-summary-date-ranges
"""

from __future__ import annotations

import argparse
import json
import shlex
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from backend.configuration import atomic_write_text

from .containers import TaskContainer, cache_root, command
from .import_public_suite import DATA_REV, SWE_REV, TB_REV, check_revision
from .tasks import resolve_tasks
from .upstream import grade, prepare_environment, run_oracle, task_source


def prepare_task(task, cache: Path, cancelled=None) -> dict:
    receipts = cache / "prepared"
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / f"{task.name}.json"
    container = TaskContainer(task, cancelled, cache=cache)
    source_image = container.prepare_image(prepared=False)
    environment_version = {"financial-document-processor": 5, "headless-terminal": 3}.get(
        task.container.get("task_path"), 2
    )
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (
            previous.get("prepared")
            and previous.get("suite_version") == task.suite_version
            and previous.get("environment_version") == environment_version
        ):
            code, _ = container.docker("image", "inspect", previous["image_id"], check=False)
            if code == 0:
                return previous
    report = {
        "task": task.name,
        "suite_version": task.suite_version,
        "image_id": source_image,
        "source_image_id": source_image,
        "prepared": True,
        "verified": False,
        "environment_version": environment_version,
    }
    if task.container["kind"] == "terminal_bench":
        # Prime exactly the package set requested by the pinned test launcher.
        # Do not execute or copy any tests/solutions into this reusable image.
        script = (task_source(container) / "tests" / "test.sh").read_text(encoding="utf-8").replace("\\\n", " ")
        uv_line = next((line.strip() for line in script.splitlines() if line.strip().startswith("uvx ")), None)
        pip_line = next((line.strip() for line in script.splitlines() if line.strip().startswith("pip install ")), None)
        try:
            container.start(image=source_image, network=True)
            container.exec("apt-get update && apt-get install -y curl ca-certificates", timeout=600)
            for line in script.splitlines():
                if line.strip().startswith("apt-get install "):
                    container.exec(line.strip(), timeout=600)
            if uv_line:
                args = shlex.split(uv_line)
                if "pytest" not in args:
                    raise RuntimeError("Unsupported upstream verifier setup; do not guess dependencies.")
                prefix = shlex.join(args[: args.index("pytest") + 1])
                container.exec("curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh", timeout=180)
                container.exec("source $HOME/.local/bin/env && " + prefix + " --version", timeout=600)
            elif pip_line:
                container.exec(pip_line, timeout=600)
            else:
                raise RuntimeError("Unsupported upstream verifier dependency setup.")
            if task.container["task_path"] == "financial-document-processor":
                # Cache only the original solution's declared OCR dependencies,
                # never its code or outputs. Execution remains fully offline.
                solution = (task_source(container) / "solution" / "solve.sh").read_text(encoding="utf-8")
                metadata = solution.split("# /// script\n", 1)[1].split("# ///", 1)[0]
                dependencies = tomllib.loads("\n".join(line.removeprefix("# ") for line in metadata.splitlines()))[
                    "dependencies"
                ]
                container.exec("apt-get update && apt-get install -y tesseract-ocr=5.3.4-1build5", timeout=600)
                packages = " ".join("--with " + shlex.quote(value) for value in dependencies)
                container.exec("uv run -p 3.13 " + packages + " python -c 'pass'", timeout=600)
            container.exec(
                'printf \'Acquire::Retries "0";\\nAcquire::http::Timeout "2";\\nAcquire::https::Timeout "2";\\n\' > /etc/apt/apt.conf.d/99benchmark-timeouts'
            )
            _, image = container.docker(
                "commit", container.name, "mini-agent-benchmark:prepared-" + task.name, timeout=120
            )
            report["image_id"] = image.strip()
        finally:
            container.close()
    atomic_write_text(path, json.dumps(report, indent=2))
    return report


def download_sources(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    repositories = (
        ("terminal-bench-2", "https://github.com/harbor-framework/terminal-bench-2.git", TB_REV),
        ("swe-bench-pro", "https://github.com/scaleapi/SWE-bench_Pro-os.git", SWE_REV),
    )
    for name, url, revision in repositories:
        root = cache / name
        if not root.exists():
            command(["git", "-c", "core.autocrlf=false", "clone", "--no-checkout", url, str(root)], timeout=900)
            command(["git", "-C", str(root), "config", "core.autocrlf", "false"])
            command(["git", "-C", str(root), "checkout", "--detach", revision], timeout=120)
        check_revision(root, revision)
    parquet = cache / f"swe-pro-{DATA_REV}.parquet"
    if not parquet.exists():
        url = (
            f"https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/{DATA_REV}/data/test-00000-of-00001.parquet"
        )
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read(50_000_001)
        if len(data) > 50_000_000:
            raise RuntimeError("Unexpectedly large dataset download.")
        parquet.write_bytes(data)
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Source preparation requires: uv run --with pyarrow==25.0.1 python -m benchmarks.prepare --sources"
        ) from exc
    selected = {
        task.source.task_id
        for task in resolve_tasks([])
        if task.container and task.container["kind"] == "swe_bench_pro"
    }
    rows = {row["instance_id"]: row for row in pq.read_table(parquet).to_pylist() if row["instance_id"] in selected}
    if set(rows) != selected:
        raise RuntimeError("Pinned dataset is missing selected tasks.")
    (cache / "swe-selected.json").write_text(json.dumps(rows), encoding="utf-8")


def validate_task(task, cache: Path) -> dict:
    report = {
        **prepare_task(task, cache),
        "verified": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "baseline": None,
        "oracle": None,
    }
    report.pop("error", None)
    for oracle in (False, True):
        container = TaskContainer(task, cache=cache)
        try:
            prepare_environment(container)
            report["image_id"] = container.image
            if oracle:
                run_oracle(container)
            verdicts = grade(container)
            passed = bool(verdicts) and all(verdict.score == 1 for verdict in verdicts)
            report["oracle" if oracle else "baseline"] = {
                "passed": passed,
                "verdicts": [vars(verdict) for verdict in verdicts],
            }
        except Exception as exc:
            from backend.domain import safe_error_message

            report["error"] = safe_error_message(exc)
            break
        finally:
            container.close()
    report["verified"] = bool(
        report["baseline"] and report["oracle"] and not report["baseline"]["passed"] and report["oracle"]["passed"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", action="store_true")
    parser.add_argument(
        "--verify", action="store_true", help="Run baseline and original reference solution; no model calls."
    )
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--cache", type=Path, default=cache_root())
    args = parser.parse_args()
    if args.sources:
        download_sources(args.cache)
        print("Pinned public source data prepared.")
        if not args.verify and not args.task:
            return 0
    receipts = args.cache / "prepared"
    receipts.mkdir(parents=True, exist_ok=True)
    failed = False
    for task in resolve_tasks(args.task):
        if task.container is None:
            continue
        if args.verify:
            try:
                report = validate_task(task, args.cache)
            except Exception as exc:
                from backend.domain import safe_error_message

                report = {
                    "task": task.name,
                    "suite_version": task.suite_version,
                    "verified": False,
                    "error": safe_error_message(exc),
                }
            failed |= not report["verified"]
        else:
            try:
                report = prepare_task(task, args.cache)
            except Exception as exc:
                from backend.domain import safe_error_message

                report = {"task": task.name, "error": safe_error_message(exc), "verified": False}
                failed = True
        receipt = receipts / f"{task.name}.json"
        atomic_write_text(receipt, json.dumps(report, indent=2))
        print(json.dumps(report, ensure_ascii=True), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
