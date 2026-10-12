"""SSH execution helpers with dry-run-first behavior."""

from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

from deploy.lib.config import TargetConfig


def ssh_command(target: TargetConfig) -> list[str]:
    return ["ssh", "-p", str(target.ssh_port), target.ssh_destination]


def print_remote_plan(target: TargetConfig, label: str, script: str) -> None:
    print(f"[dry-run] target={target.name} host={target.host} action={label}")
    for line in script.splitlines():
        print(f"    {line}")


def run_remote(
    target: TargetConfig, script: str, *, execute: bool, label: str
) -> None:
    if not execute:
        print_remote_plan(target, label, script)
        return
    print(f"[execute] target={target.name} host={target.host} action={label}")
    subprocess.run(
        [*ssh_command(target), "bash", "-s"],
        input=script,
        text=True,
        encoding="utf-8",
        check=True,
    )


def capture_remote(
    target: TargetConfig, command: str, *, execute: bool, label: str
) -> str:
    if not execute:
        print_remote_plan(target, label, command)
        return ""
    result = subprocess.run(
        [*ssh_command(target), command],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return result.stdout


def make_remote_directory(
    target: TargetConfig, path: str, *, execute: bool
) -> None:
    script = f"mkdir -p {shlex.quote(path)}"
    run_remote(
        target, script, execute=execute, label=f"create directory {path}"
    )


def write_remote_file(
    target: TargetConfig,
    local_path: Path,
    remote_path: str,
    *,
    execute: bool,
) -> None:
    if not execute:
        print(
            f"[dry-run] target={target.name} "
            f"write {local_path.as_posix()} -> {remote_path}"
        )
        return
    remote_command = f"cat > {shlex.quote(remote_path)}"
    with local_path.open("rb") as source:
        subprocess.run(
            [*ssh_command(target), remote_command],
            stdin=source,
            check=True,
        )
