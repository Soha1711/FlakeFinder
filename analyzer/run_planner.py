"""
analyzer/run_planner.py — Compatibility shim re-exporting from root run_planner.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from run_planner import run, run_planner, main

__all__ = ["run", "run_planner", "main"]
