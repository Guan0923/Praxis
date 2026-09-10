"""Run only in the isolated benchmark data environment."""

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq


def main() -> None:
    source, task_id, destination = sys.argv[1:]
    rows = pq.read_table(source, filters=[("instance_id", "=", task_id)]).to_pylist()
    if len(rows) != 1:
        raise RuntimeError("Pinned dataset does not contain the selected task.")
    Path(destination).write_text(json.dumps({task_id: rows[0]}), encoding="utf-8")


if __name__ == "__main__":
    main()
