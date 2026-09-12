"""Import pinned public tasks. Requires pyarrow only for importing, not running."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path

TB_REV = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
SWE_REV = "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
DATA_REV = "7ab5114912baf22bb098818e604c02fe7ad2c11f"
VERSION = "public-30-2026-09-09"
TERMINAL = (
    "schemelike-metacircular-eval",
    "cancel-async-tasks",
    "custom-memory-heap-crash",
    "git-leak-recovery",
    "sanitize-git-repo",
    "polyglot-c-py",
    "polyglot-rust-c",
    "write-compressor",
    "headless-terminal",
    "circuit-fibsqrt",
)
DATA = (
    "db-wal-recovery",
    "extract-elf",
    "financial-document-processor",
    "large-scale-text-editing",
    "log-summary-date-ranges",
    "multi-source-data-merger",
    "constraints-scheduling",
    "regex-log",
    "sqlite-db-truncate",
    "sparql-university",
)
SWE_PREFIXES = (
    "instance_ansible__ansible-a26c325",
    "instance_ansible__ansible-b748ede",
    "instance_ansible__ansible-d58e69c",
    "instance_ansible__ansible-1c06c46",
    "instance_ansible__ansible-e40889e",
    "instance_qutebrowser__qutebrowser-c580ebf",
    "instance_qutebrowser__qutebrowser-0fc6d11",
    "instance_qutebrowser__qutebrowser-fd6790f",
    "instance_qutebrowser__qutebrowser-44e6419",
    "instance_qutebrowser__qutebrowser-ed19d7f",
)


def check_revision(root: Path, revision: str) -> None:
    if not root.is_dir():
        raise RuntimeError(f"Benchmark source directory is missing: {root}. Download task resources first.")
    if not (root / ".git").exists():
        raise RuntimeError(f"Benchmark source is not a Git repository: {root}")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or is unavailable to the backend.") from exc
    if result.returncode:
        raise RuntimeError(f"Cannot read benchmark source revision: {result.stderr.strip()[:2000]}")
    actual = result.stdout.strip()
    if actual != revision:
        raise ValueError(f"Expected pinned revision {revision}, got {actual}")


def display_title(statement: str) -> str:
    try:
        decoded = json.loads(statement)
        if isinstance(decoded, str):
            statement = decoded
    except ValueError:
        pass
    for line in statement.splitlines():
        text = line.strip(' #*"')
        if text and text.lower().rstrip(":") not in {"title", "issue title"}:
            return text[:240]
    return "Repository repair"


def generate(cache: Path, output: Path) -> None:
    import pyarrow.parquet as parquet

    tb, swe = cache / "terminal-bench-2", cache / "swe-bench-pro"
    check_revision(tb, TB_REV)
    check_revision(swe, SWE_REV)
    tasks = []
    for capability, names in (("terminal", TERMINAL), ("data_processing", DATA)):
        for name in names:
            root = tb / name
            spec = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
            env = spec["environment"]
            tasks.append(
                {
                    "name": f"tb2-{name}",
                    "description": spec["task"]["description"],
                    "capability": capability,
                    "difficulty": spec["metadata"]["difficulty"],
                    "prompt": (root / "instruction.md").read_text(encoding="utf-8"),
                    "tags": spec["metadata"].get("tags", []),
                    "source": {
                        "benchmark": "Terminal-Bench 2.0",
                        "task_id": name,
                        "url": f"https://github.com/harbor-framework/terminal-bench-2/tree/{TB_REV}/{name}",
                        "source_revision": TB_REV,
                        "license": "Apache-2.0",
                        "adaptation_notes": "Original prompt, environment and verifier; curated subset, not an official leaderboard score.",
                    },
                    "container": {
                        "kind": "terminal_bench",
                        "task_path": name,
                        "image": env["docker_image"],
                        "cpus": env.get("cpus", 1),
                        "memory_mb": env["memory_mb"],
                        "build_seconds": env.get("build_timeout_sec", 600),
                        "verifier_seconds": spec["verifier"].get("timeout_sec", 900),
                    },
                }
            )
    rows = parquet.read_table(cache / f"swe-pro-{DATA_REV}.parquet").to_pylist()
    private_rows = {}
    for prefix in SWE_PREFIXES:
        matches = [r for r in rows if r["instance_id"].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one source task for {prefix}")
        row = matches[0]
        identifier = row["instance_id"]
        private_rows[identifier] = row
        if not (swe / "run_scripts" / identifier / "parser.py").exists():
            raise ValueError(f"Missing upstream verifier: {identifier}")
        tasks.append(
            {
                "name": "swepro-" + identifier.removeprefix("instance_").split("-v")[0],
                "description": f"{row['repo']}: {display_title(row['problem_statement'])}",
                "capability": "software_engineering",
                "difficulty": "hard",
                "prompt": f"{row['problem_statement']}\n\nRequirements:\n{row['requirements']}\n\nNew interfaces introduced:\n{row['interface']}",
                "tags": [row["repo"], row["repo_language"]],
                "source": {
                    "benchmark": "SWE-bench Pro",
                    "task_id": identifier,
                    "url": f"https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/tree/{DATA_REV}",
                    "source_revision": DATA_REV,
                    "license": "See upstream dataset card and repository licenses; harness MIT",
                    "adaptation_notes": "Full repository at base_commit, original requirements and upstream tests/parser; curated public subset.",
                },
                "container": {
                    "kind": "swe_bench_pro",
                    "image": "jefzda/sweap-images:" + row["dockerhub_tag"],
                    "base_commit": row["base_commit"],
                    "harness_revision": SWE_REV,
                    "cpus": 2,
                    "memory_mb": 4096,
                    "build_seconds": 1800,
                    "verifier_seconds": 3600,
                },
            }
        )
    for task in tasks:
        spec = task["container"]
        spec["source_image_id"] = subprocess.check_output(
            ["docker", "image", "inspect", spec["image"], "--format", "{{.Id}}"], text=True
        ).strip()
        spec["platform"] = "linux/amd64"
    output.write_text(
        json.dumps({"suite_version": VERSION, "tasks": tasks}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (cache / "swe-selected.json").write_text(json.dumps(private_rows), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("public_suite.json"))
    args = parser.parse_args()
    generate(args.cache, args.output)
