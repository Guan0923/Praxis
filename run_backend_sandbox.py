"""Sandbox-local backend launcher: keep the data root inside the workspace.

The DSH sandbox denies writes outside the session workspace, so the default
``~/.praxis`` data root is unreachable.  This launcher points the data
root at ``<repo>/.praxis`` (gitignored) so the API server can run.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from backend.api.app import create_app
from backend.api.state import WebAppState

if __name__ == "__main__":
    uvicorn.run(
        create_app(WebAppState(Path(".praxis").resolve())),
        host="127.0.0.1",
        port=8000,
    )
