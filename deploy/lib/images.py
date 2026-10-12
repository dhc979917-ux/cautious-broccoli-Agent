"""Image reference helpers shared by deployment tools."""

from __future__ import annotations

import re


IMAGE_TYPES = ("backend", "frontend", "skill")
IMAGE_REPOSITORIES = {
    "backend": "taskpilot-backend",
    "frontend": "taskpilot-frontend",
    "skill": "taskpilot-skill",
}
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_tag(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"invalid image tag: {tag!r}")
    return tag


def image_ref(registry_prefix: str, image_type: str, tag: str) -> str:
    if image_type not in IMAGE_REPOSITORIES:
        raise ValueError(f"unknown image type: {image_type}")
    validate_tag(tag)
    return f"{registry_prefix}/{IMAGE_REPOSITORIES[image_type]}:{tag}"
