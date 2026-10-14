"""Minimal import bootstrap for the isolated pywin32 service host."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

if hasattr(os, "add_dll_directory"):
    for dll_dir in (
        Path(sys.base_prefix) / "Library" / "bin",
        Path(sys.base_prefix) / "DLLs",
        Path(sys.prefix) / "Library" / "bin",
        Path(sys.prefix) / "DLLs",
    ):
        if dll_dir.is_dir():
            try:
                os.add_dll_directory(str(dll_dir))
            except OSError:
                pass

try:
    with open(r"C:\ProgramData\Praxis\SandboxBroker\bootstrap.log", "a", encoding="utf-8") as _f:
        _f.write(f"Bootstrap invoked by {sys.executable}, args={sys.argv}\n")
except Exception:
    pass


def _install_backend_package() -> None:
    if "backend" in sys.modules:
        return
    source_root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "backend",
        source_root / "__init__.py",
        submodule_search_locations=[str(source_root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Broker backend package bootstrap failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backend"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("backend", None)
        raise


_install_backend_package()

from backend.sandbox.service_main import PraxisSandboxBrokerService  # noqa: E402

__all__ = ["PraxisSandboxBrokerService"]
