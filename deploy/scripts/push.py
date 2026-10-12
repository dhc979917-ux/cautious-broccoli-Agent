#!/usr/bin/env python3
"""Push built TaskPilot images to the private registry."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys
from _bootstrap import REPO_ROOT

from deploy.lib.cli import add_execution_argument, add_image_selection, add_target_argument, selected_images
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.git import resolve_commit
from deploy.lib.images import IMAGE_TYPES, image_ref, validate_tag
from deploy.lib.remote import run_remote


def quote(value: str) -> str:
    return shlex.quote(value)


def resolve_tags(args: argparse.Namespace, short_commit: str) -> dict[str, str]:
    default_tag = args.tag or short_commit
    return {
        image_type: validate_tag(getattr(args, f"{image_type}_tag") or default_tag)
        for image_type in IMAGE_TYPES
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="52")
    add_image_selection(parser)
    parser.add_argument("--tag", help="default tag for selected images (default: short HEAD)")
    for image_type in IMAGE_TYPES:
        parser.add_argument(f"--{image_type}-tag", help=f"{image_type} image tag")
    add_execution_argument(parser)
    args = parser.parse_args()

    _, short_commit = resolve_commit(REPO_ROOT, "HEAD")
    tags = resolve_tags(args, short_commit)
    images = selected_images(args.image)
    for name in parse_target_selection(args.target):
        target = load_target(name)
        if not target.has_role("registry"):
            raise SystemExit(f"target {target.name} is not a registry host")
        sections = []
        for image_type in images:
            ref = image_ref(target.registry_prefix, image_type, tags[image_type])
            sections.append(
                f"docker image inspect {quote(ref)} >/dev/null\n"
                f"docker push {quote(ref)}"
            )
        script = "set -euo pipefail\n" + "\n".join(sections) + "\n"
        run_remote(
            target,
            script,
            execute=args.execute,
            label=f"push {', '.join(images)}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
