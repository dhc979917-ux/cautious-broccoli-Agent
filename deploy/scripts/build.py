#!/usr/bin/env python3
"""Build immutable TaskPilot images on the configured build host."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys
from _bootstrap import REPO_ROOT

from deploy.lib.cli import add_execution_argument, add_image_selection, add_target_argument, selected_images
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.git import has_uncommitted_changes, resolve_commit
from deploy.lib.images import IMAGE_TYPES, image_ref, validate_tag
from deploy.lib.remote import run_remote


def quote(value: str) -> str:
    return shlex.quote(value)


def resolve_tags(args: argparse.Namespace, short_commit: str) -> dict[str, str]:
    default_tag = args.tag or short_commit
    tags = {}
    for image_type in IMAGE_TYPES:
        value = getattr(args, f"{image_type}_tag") or default_tag
        tags[image_type] = validate_tag(value)
    return tags


def build_script(target, commit: str, tags: dict[str, str], images: list[str], pull: bool) -> str:
    refs = {
        image_type: image_ref(target.registry_prefix, image_type, tags[image_type])
        for image_type in IMAGE_TYPES
    }
    assignments = [
        f"REPO={quote(target.repo_path)}",
        f"COMMIT={quote(commit)}",
        "TMP_DIR=$(mktemp -d)",
    ]
    prefix = "\n".join(assignments)
    pull_argument = "--pull" if pull else ""
    sections = []

    if "backend" in images:
        sections.append(f"""
BACKEND_CONTEXT="$TMP_DIR/backend"
mkdir -p "$BACKEND_CONTEXT"
git -C "$REPO" archive --format=tar "$COMMIT" backend | tar -xf - -C "$BACKEND_CONTEXT" --strip-components=1
docker build {pull_argument} -f "$BACKEND_CONTEXT/Dockerfile" \\
  --build-arg VCS_REF="$COMMIT" --build-arg VERSION={quote(tags['backend'])} \\
  -t {quote(refs['backend'])} "$BACKEND_CONTEXT"
""")
    if "frontend" in images:
        sections.append(f"""
ROOT_CONTEXT="$TMP_DIR/frontend"
mkdir -p "$ROOT_CONTEXT"
git -C "$REPO" archive --format=tar "$COMMIT" | tar -xf - -C "$ROOT_CONTEXT"
docker build {pull_argument} -f "$ROOT_CONTEXT/frontend/Dockerfile" \\
  --build-arg VCS_REF="$COMMIT" --build-arg VERSION={quote(tags['frontend'])} \\
  -t {quote(refs['frontend'])} "$ROOT_CONTEXT"
""")
    if "skill" in images:
        sections.append(f"""
ROOT_CONTEXT="$TMP_DIR/skill"
mkdir -p "$ROOT_CONTEXT"
git -C "$REPO" archive --format=tar "$COMMIT" | tar -xf - -C "$ROOT_CONTEXT"
docker build {pull_argument} -f "$ROOT_CONTEXT/docker/Dockerfile.skill" \\
  --build-arg VCS_REF="$COMMIT" --build-arg VERSION={quote(tags['skill'])} \\
  -t {quote(refs['skill'])} "$ROOT_CONTEXT"
""")
    image_lines = "\n".join(f"docker image inspect {quote(refs[image_type])}" for image_type in images)
    return f"""set -euo pipefail
{prefix}
trap 'rm -rf "$TMP_DIR"' EXIT
git -C "$REPO" fetch --all --prune
git -C "$REPO" cat-file -e "$COMMIT^{{commit}}"
{''.join(sections)}
{image_lines}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="52")
    add_image_selection(parser)
    parser.add_argument("--commit", default="HEAD", help="Git commit to build (default: HEAD)")
    parser.add_argument("--tag", help="default tag for selected images (default: short commit)")
    for image_type in IMAGE_TYPES:
        parser.add_argument(f"--{image_type}-tag", help=f"{image_type} image tag")
    parser.add_argument("--no-pull", action="store_true", help="do not refresh base images")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow execution even when the local working tree is dirty",
    )
    add_execution_argument(parser)
    args = parser.parse_args()

    full_commit, short_commit = resolve_commit(REPO_ROOT, args.commit)
    tags = resolve_tags(args, short_commit)
    images = selected_images(args.image)
    for name in parse_target_selection(args.target):
        target = load_target(name)
        if not target.has_role("build"):
            raise SystemExit(f"target {target.name} is not a build host")
        if target.repo_path is None:
            raise SystemExit(f"target {target.name} has no repository path")
        if args.execute and has_uncommitted_changes(REPO_ROOT) and not args.allow_dirty:
            raise SystemExit(
                "local working tree is dirty; commit and push first, or pass --allow-dirty"
            )
        run_remote(
            target,
            build_script(target, full_commit, tags, images, not args.no_pull),
            execute=args.execute,
            label=f"build {', '.join(images)}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
