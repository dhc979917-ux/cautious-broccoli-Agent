"""Local deployment state mirrors; authoritative state lives on each target."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deploy.lib.images import IMAGE_TYPES


DEPLOY_ROOT = Path(__file__).resolve().parents[1]


def local_state_path(target_name: str) -> Path:
    return DEPLOY_ROOT / "state" / f"{target_name}-state.json"


def read_local_state(target_name: str) -> dict[str, Any] | None:
    path = local_state_path(target_name)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_local_state(target_name: str, state: dict[str, Any]) -> None:
    path = local_state_path(target_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def current_images(state: dict[str, Any] | None) -> dict[str, str] | None:
    if not state:
        return None
    current = state.get("current")
    if not isinstance(current, dict):
        return None
    images = current.get("images")
    if not isinstance(images, dict):
        return None
    result = {}
    for image_type in IMAGE_TYPES:
        value = images.get(image_type)
        if not isinstance(value, dict) or not isinstance(value.get("tag"), str):
            return None
        result[image_type] = value["tag"]
    return result
