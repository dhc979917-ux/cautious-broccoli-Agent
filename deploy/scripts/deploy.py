#!/usr/bin/env python3
"""Deploy pinned image tags to one or more target servers."""

from __future__ import annotations

import argparse
from _bootstrap import REPO_ROOT

from deploy.lib.cli import add_execution_argument, add_target_argument
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.deployment import deploy_images, read_remote_state
from deploy.lib.git import resolve_commit
from deploy.lib.images import IMAGE_TYPES, validate_tag
from deploy.lib.state import current_images, read_local_state


def resolve_tags(args: argparse.Namespace, target, short_commit: str, state) -> dict[str, str]:
    existing = current_images(state) or {}
    default_tag = args.tag or short_commit
    tags = {}
    for image_type in IMAGE_TYPES:
        value = getattr(args, f"{image_type}_tag")
        if not value:
            value = default_tag
        if not value:
            value = existing.get(image_type)
        if not value:
            raise SystemExit(
                f"target {target.name}: --{image_type}-tag is required when no current state exists"
            )
        tags[image_type] = validate_tag(value)
    return tags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="54")
    parser.add_argument("--commit", help="Git commit represented by the image tags")
    parser.add_argument("--tag", help="default tag for all images")
    parser.add_argument(
        "--allow-mismatched-tags",
        action="store_true",
        help="allow backend and frontend image tags to differ (advanced use only)",
    )
    for image_type in IMAGE_TYPES:
        parser.add_argument(f"--{image_type}-tag", help=f"{image_type} image tag")
    parser.add_argument(
        "--write-local-state",
        action="store_true",
        help="mirror the non-secret deployment state into deploy/state after execution",
    )
    add_execution_argument(parser)
    args = parser.parse_args()

    short_commit = ""
    if args.commit:
        _, short_commit = resolve_commit(REPO_ROOT, args.commit)
    if args.tag:
        args.tag = validate_tag(args.tag)

    for name in parse_target_selection(args.target):
        target = load_target(name)
        state = read_remote_state(target, execute=args.execute) if args.execute else read_local_state(name)
        tags = resolve_tags(args, target, short_commit, state)
        if tags["backend"] != tags["frontend"] and not args.allow_mismatched_tags:
            raise SystemExit(
                f"target {target.name}: backend and frontend image tags differ "
                f"(backend={tags['backend']}, frontend={tags['frontend']}); "
                "use the same release tag or pass --allow-mismatched-tags"
            )
        deploy_images(
            target,
            tags,
            commit=args.commit,
            execute=args.execute,
            write_local_mirror=args.write_local_state,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
