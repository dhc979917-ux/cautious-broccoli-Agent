"""Local Git helpers used before remote builds."""

from __future__ import annotations

from pathlib import Path
import subprocess


def run_git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return result.stdout.strip()


def resolve_commit(repo_root: Path, commit: str) -> tuple[str, str]:
    full_commit = run_git(
        ["rev-parse", "--verify", f"{commit}^{{commit}}"], repo_root
    )
    short_commit = run_git(["rev-parse", "--short", full_commit], repo_root)
    return full_commit, short_commit


def has_uncommitted_changes(repo_root: Path) -> bool:
    status = run_git(["status", "--porcelain"], repo_root)
    return bool(status)
