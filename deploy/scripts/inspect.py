#!/usr/bin/env python3
"""Inspect deployment state, containers, and health on a target server."""

from __future__ import annotations

import argparse
import shlex
import _bootstrap  # noqa: F401

from deploy.lib.cli import add_execution_argument, add_target_argument
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.deployment import compose_command
from deploy.lib.remote import run_remote


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="54")
    add_execution_argument(parser)
    args = parser.parse_args()

    for name in parse_target_selection(args.target):
        target = load_target(name)
        script = f"""set -euo pipefail
echo '--- deployment state ---'
cat {shlex.quote(target.state_file)} || true
echo '--- compose services ---'
{compose_command(target, 'ps')}
echo '--- health ---'
curl -fsS {shlex.quote(target.health_url)}
echo
"""
        run_remote(target, script, execute=args.execute, label="inspect deployment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
