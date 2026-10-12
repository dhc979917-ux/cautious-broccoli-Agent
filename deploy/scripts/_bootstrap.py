"""Prepare imports when a deployment script is invoked by file path."""

from __future__ import annotations

from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]

resolved_path = [Path(item).resolve() for item in sys.path]
sys.path[:] = [
    item
    for item, resolved in zip(sys.path, resolved_path)
    if resolved != SCRIPT_DIR
]
sys.path.insert(0, REPO_ROOT.as_posix())
