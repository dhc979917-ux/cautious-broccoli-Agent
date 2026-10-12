#!/usr/bin/env python3
"""Clear and rebuild AgentScope derived sandbox images on one target."""

from __future__ import annotations

import argparse
import shlex
import _bootstrap  # noqa: F401

from deploy.lib.cli import add_execution_argument, add_target_argument
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.deployment import read_remote_state, sync_runtime_files
from deploy.lib.images import image_ref, validate_tag
from deploy.lib.remote import run_remote
from deploy.lib.state import current_images, read_local_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="54")
    parser.add_argument("--skill-tag", help="skill base image tag (default: current state)")
    parser.add_argument(
        "--backend-container",
        help="backend container used to generate the AgentScope build context",
    )
    add_execution_argument(parser)
    args = parser.parse_args()

    for name in parse_target_selection(args.target):
        target = load_target(name)
        if not target.has_role("deploy"):
            raise SystemExit(f"target {target.name} cannot run a deployment sandbox")
        tag = args.skill_tag
        if not tag:
            state = read_remote_state(target, execute=args.execute) if args.execute else read_local_state(name)
            images = current_images(state)
            tag = images.get("skill") if images else None
        if not tag:
            if not args.execute:
                print(
                    f"[dry-run] target={target.name} will read the current skill tag "
                    "from remote state"
                )
                continue
            raise SystemExit(f"target {target.name}: --skill-tag is required when no state exists")
        tag = validate_tag(tag)
        ref = image_ref(target.registry_prefix, "skill", tag)
        backend_container = args.backend_container or target.backend_container
        sync_runtime_files(target, execute=args.execute)
        script = f"""set -euo pipefail
docker pull {shlex.quote(ref)}
WORKSPACE_DOCKER_IMAGE={shlex.quote(ref)} \\
BACKEND_CONTAINER={shlex.quote(backend_container)} \\
bash {shlex.quote(f'{target.compose_dir}/rebuild_sandbox_image.sh')}
"""
        run_remote(
            target,
            script,
            execute=args.execute,
            label="refresh AgentScope sandbox cache",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
