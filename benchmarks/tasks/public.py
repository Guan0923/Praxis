"""Fixed public tasks. Loading metadata never downloads or starts containers."""

import json
from pathlib import Path

from ..model import BenchmarkTask, Budgets, SourceMetadata

_manifest = json.loads((Path(__file__).parents[1] / "public_suite.json").read_text(encoding="utf-8"))
TASKS = tuple(
    BenchmarkTask(
        name=item["name"],
        description=item["description"],
        capability=item["capability"],
        difficulty=item["difficulty"],
        prompt=item["prompt"],
        source=SourceMetadata(**item["source"]),
        tags=tuple(item["tags"]),
        budgets=Budgets(None, item["container"]["agent_seconds"]),
        container=item["container"],
        suite_version=_manifest["suite_version"],
    )
    for item in _manifest["tasks"]
)
