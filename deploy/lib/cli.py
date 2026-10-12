"""Common command-line behavior for deployment tools."""

from __future__ import annotations

import argparse


def add_target_argument(
    parser: argparse.ArgumentParser, default: str | None = None
) -> None:
    parser.add_argument(
        "--target",
        default=default,
        help="target name, comma-separated list, or all (default: %(default)s)",
    )


def add_execution_argument(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="print the remote plan without connecting to a server (default)",
    )
    group.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="execute the remote plan",
    )
    parser.set_defaults(execute=False)


def add_image_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--image",
        choices=("backend", "frontend", "skill"),
        action="append",
        help="image to build or push; repeat for multiple images (default: all)",
    )


def selected_images(values: list[str] | None) -> list[str]:
    return list(values or ("backend", "frontend", "skill"))
