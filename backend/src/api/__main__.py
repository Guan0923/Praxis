"""Run the web app: ``uv run python -m backend.api``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .state import DEFAULT_DATA_ROOT, WebAppState


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Praxis backend.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Long-lived SSE responses cannot drain before application cleanup starts.
    uvicorn.run(
        create_app(WebAppState(args.data_root)),
        host="127.0.0.1",
        port=args.port,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
