"""Execute upstream solutions/verifiers without exposing them to the model."""

from __future__ import annotations

import ast
import json
import shlex
import subprocess
from pathlib import Path

from .containers import TaskContainer
from .import_public_suite import SWE_REV, TB_REV, check_revision
from .model import CheckerVerdict


def upload_upstream(container: TaskContainer, relative: str, destination: str) -> None:
    """Export pinned files with LF, independent of host Git newline settings."""
    terminal = container.spec["kind"] == "terminal_bench"
    root = container.cache / ("terminal-bench-2" if terminal else "swe-bench-pro")
    revision = TB_REV if terminal else SWE_REV
    base = container.spec["task_path"] if terminal else "run_scripts/" + container.task.source.task_id
    if relative:
        base += "/" + relative
    archive = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.eol=lf",
            "archive",
            "--format=tar",
            revision + ":" + base,
        ],
        capture_output=True,
        timeout=60,
        check=True,
    ).stdout
    if len(archive) > 50_000_000:
        raise RuntimeError("Unexpectedly large upstream verifier archive.")
    directory = shlex.quote(destination)
    container.exec(f"mkdir -p {directory} && tar -xf - -C {directory}", data=archive)


def task_source(container: TaskContainer) -> Path:
    if container.spec["kind"] == "terminal_bench":
        root = container.cache / "terminal-bench-2"
        check_revision(root, TB_REV)
        return root / container.spec["task_path"]
    root = container.cache / "swe-bench-pro"
    check_revision(root, SWE_REV)
    return root / "run_scripts" / container.task.source.task_id


def swe_row(container: TaskContainer) -> dict:
    path = container.cache / "swe-rows" / f"{container.task.name}.json"
    if not path.exists():
        raise RuntimeError("Public dataset cache is missing. Run python -m benchmarks.prepare --sources first.")
    return json.loads(path.read_text(encoding="utf-8"))[container.task.source.task_id]


def prepare_environment(container: TaskContainer) -> None:
    source = task_source(container)
    if not source.is_dir():
        raise RuntimeError("Pinned upstream task files are missing. Run python -m benchmarks.prepare --sources.")
    receipt = container.cache / "prepared" / f"{container.task.name}.json"
    if not receipt.is_file():
        raise RuntimeError("Benchmark environment is missing. Download task resources first.")
    prepared = json.loads(receipt.read_text(encoding="utf-8"))
    if not prepared.get("prepared") or prepared.get("suite_version") != container.task.suite_version:
        raise RuntimeError("Benchmark environment is not ready. Download task resources first.")
    container.start(allow_download=False)
    container.exec("mkdir -p /logs/verifier /logs/agent")
    if container.spec["kind"] == "swe_bench_pro":
        base = shlex.quote(container.spec["base_commit"])
        container.workdir = "/app"
        container.exec(f"git reset --hard {base} && git clean -fd && git checkout {base}")
        # Upstream images contain future commits, including the answer. The model
        # gets the complete base tree but not that Git object database.
        container.exec(
            "rm -rf /app/.git && git init -q && git config user.email benchmark@localhost "
            "&& git config user.name Benchmark && git add -A && git commit -qm baseline"
        )
    # Refuse an image accidentally published with the grading artifacts included.
    code, _ = container.exec("test ! -e /tests && test ! -e /solution", check=False)
    if code:
        raise RuntimeError("Upstream image contains hidden grading/solution paths; refusing to expose it.")


def run_oracle(container: TaskContainer) -> None:
    task_source(container)
    if container.spec["kind"] == "terminal_bench":
        upload_upstream(container, "solution", "/solution")
        container.exec("bash /solution/solve.sh", timeout=container.spec["agent_seconds"])
        container.exec("rm -rf /solution")
    else:
        row = swe_row(container)
        container.exec("git apply --allow-empty -", data=row["patch"].encode("utf-8"))


def _names(value: str | list) -> list[str]:
    parsed = ast.literal_eval(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or any(not isinstance(name, str) for name in parsed):
        raise ValueError("Invalid upstream test name list.")
    return parsed


def grade(container: TaskContainer) -> list[CheckerVerdict]:
    container.tools_open = False
    if container.spec["kind"] == "terminal_bench":
        return _grade_terminal(container)
    return _grade_swe(container)


def _grade_terminal(container: TaskContainer) -> list[CheckerVerdict]:
    task_source(container)
    # A new container prevents leftover agent processes from reading the verifier
    # when it is injected. Only the agent filesystem changes are transferred.
    _, snapshot = container.docker("commit", container.name, timeout=120)
    image = snapshot.strip()
    container.close()
    verifier = TaskContainer(container.task, container.cancelled, cache=container.cache)
    try:
        verifier.start(image=image)
        verifier.exec(
            "mkdir -p /logs/verifier && rm -f /logs/verifier/reward.txt /logs/verifier/reward.json /logs/verifier/ctrf.json"
        )
        upload_upstream(verifier, "tests", "/tests")
        _, diagnostics = verifier.exec("bash /tests/test.sh", timeout=container.spec["verifier_seconds"], check=False)
        code, raw = verifier.exec("cat /logs/verifier/reward.txt", check=False)
        if code:
            code, raw = verifier.exec("cat /logs/verifier/reward.json", check=False)
            if code:
                raise RuntimeError(
                    "Original verifier produced no reward (environment or grading failure): " + diagnostics[-2500:]
                )
            values = json.loads(raw)
            if not isinstance(values, dict) or not values:
                raise ValueError("Invalid upstream reward.")
            return [CheckerVerdict(float(value), f"upstream reward: {key}") for key, value in values.items()]
        score = float(raw.strip())
        if not 0 <= score <= 1:
            raise ValueError("Upstream reward is outside [0, 1].")
        if score == 0:
            code, report = verifier.exec("cat /logs/verifier/ctrf.json", check=False)
            if code or not json.loads(report).get("results", {}).get("tests"):
                raise RuntimeError(
                    "Verifier did not execute its tests; this is an environment error, not a zero score: "
                    + diagnostics[-3000:]
                )
        return [CheckerVerdict(score, "Original Terminal-Bench verifier reward")]
    finally:
        try:
            verifier.close()
        finally:
            # Only the exact temporary image created above is removed.
            from .containers import command

            command(["docker", "image", "rm", "--no-prune", image], timeout=30)


def _grade_swe(container: TaskContainer) -> list[CheckerVerdict]:
    row = swe_row(container)
    task_source(container)
    container.exec("git add -A")
    _, prediction = container.exec("git diff --cached --binary HEAD")
    if "[output truncated]" in prediction:
        raise RuntimeError("Patch exceeds the benchmark transfer limit.")
    container.close()
    verifier = TaskContainer(container.task, container.cancelled, cache=container.cache)
    try:
        verifier.start(image=container.image)
        verifier.workdir = "/app"
        base = shlex.quote(container.spec["base_commit"])
        verifier.exec(f"git reset --hard {base} && git clean -fd && git checkout {base}")
        verifier.exec("git apply --allow-empty -", data=prediction.encode("utf-8"))
        # This is the upstream entryscript ordering: prediction first, then the
        # original hidden tests restored from the upstream fix commit.
        verifier.exec(row["before_repo_set_cmd"].strip().splitlines()[-1])
        verifier.exec("mkdir -p /workspace")
        upload_upstream(verifier, "", "/workspace")
        tests = shlex.quote(",".join(_names(row["selected_test_files_to_run"])))
        verifier.exec(
            f"bash /workspace/run_script.sh {tests} > /workspace/stdout.log 2> /workspace/stderr.log",
            timeout=container.spec["verifier_seconds"],
            check=False,
        )
        verifier.exec("python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json")
        _, text = verifier.exec("cat /workspace/output.json")
        output = json.loads(text)
        actual = {item["name"]: item["status"] for item in output["tests"]}
        required = _names(row["fail_to_pass"]) + _names(row["pass_to_pass"])
        if not actual or not required:
            raise RuntimeError("Original SWE-bench Pro parser found no tests.")
        missing = set(required) - actual.keys()
        passed = sum(actual.get(name) == "PASSED" for name in required)
        return [
            CheckerVerdict(
                float(passed == len(required)),
                f"SWE-bench Pro required tests: {passed}/{len(required)}; not reported: {len(missing)}",
            )
        ]
    finally:
        verifier.close()
