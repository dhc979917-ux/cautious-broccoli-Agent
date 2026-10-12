#!/usr/bin/env python3
"""Roll a target back to the image tags in its remote deployment state."""

from __future__ import annotations

import argparse
import _bootstrap  # noqa: F401

from deploy.lib.cli import add_execution_argument, add_target_argument
from deploy.lib.config import load_target, parse_target_selection
from deploy.lib.deployment import rollback_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_argument(parser, default="54")
    parser.add_argument(
        "--write-local-state",
        action="store_true",
        help="mirror the non-secret rollback state into deploy/state after execution",
    )
    add_execution_argument(parser)
    args = parser.parse_args()

    for name in parse_target_selection(args.target):
        target = load_target(name)
        rollback_target(
            target,
            execute=args.execute,
            write_local_mirror=args.write_local_state,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
