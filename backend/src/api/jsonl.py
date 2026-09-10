"""Line-by-line JSON attachments without building a second full export in memory."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

from fastapi.responses import StreamingResponse


def jsonl_download(records: Iterable[Mapping[str, object]], filename: str) -> StreamingResponse:
    safe_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return StreamingResponse(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
