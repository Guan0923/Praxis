"""Summarize matching loopback pipeline runs, without conflating queue delay and write cost."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from statistics import median


def summarize(directory: Path) -> dict:
    backend = json.loads((directory / "pipeline-backend.json").read_text(encoding="utf-8"))
    browser = json.loads((directory / "pipeline-browser.json").read_text(encoding="utf-8"))
    samples: dict[str, dict] = {}
    for event in backend:
        match = re.search(r"part\d{4}", event.get("delta", ""))
        if match:
            samples.setdefault(match[0], {}).update(event)
    for event in browser["received"]:
        match = re.search(r"part\d{4}", event["delta"])
        if match:
            samples[match[0]]["browser_received"] = event["at"]
    for key, timestamp in browser["visible"].items():
        samples[key]["visible"] = timestamp
    pairs = {
        "model_to_backend": ("model_sent", "backend_delta"),
        "backend_to_publish": ("backend_delta", "display_published"),
        "model_to_browser": ("model_sent", "browser_received"),
        "browser_to_visible": ("browser_received", "visible"),
        "model_to_visible": ("model_sent", "visible"),
        "sqlite_write": ("db_started", "db_finished"),
        "backend_to_saved": ("backend_delta", "db_finished"),
    }
    result = {"samples": len(samples), "formula_replacements": browser["formulaReplacements"], "timings_ms": {}}
    for name, (start, end) in pairs.items():
        values = sorted(event[end] - event[start] for event in samples.values())
        result["timings_ms"][name] = {
            "p50": round(median(values), 2),
            "p95": round(values[int((len(values) - 1) * 0.95)], 2),
            "max": round(max(values), 2),
        }
    return result


if __name__ == "__main__":
    print(
        json.dumps(
            {
                label: summarize(Path(directory))
                for label, directory in zip(("before", "after"), sys.argv[1:], strict=True)
            },
            indent=2,
        )
    )
