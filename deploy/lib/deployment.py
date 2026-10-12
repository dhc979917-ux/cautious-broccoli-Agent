"""Reusable target deployment operations."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
from typing import Any

from deploy.lib.config import REPO_ROOT, TargetConfig
from deploy.lib.images import (
    IMAGE_REPOSITORIES,
    IMAGE_TYPES,
    image_ref,
    validate_tag,
)
from deploy.lib.remote import (
    capture_remote,
    make_remote_directory,
    run_remote,
    write_remote_file,
)
from deploy.lib.state import write_local_state


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def sync_runtime_files(target: TargetConfig, *, execute: bool) -> None:
    """Copy only deployment files; application code stays inside images."""
    compose_dir = Path(target.compose_dir)
    state_dir = Path(target.state_file).parent
    make_remote_directory(target, compose_dir.as_posix(), execute=execute)
    make_remote_directory(target, state_dir.as_posix(), execute=execute)

    write_remote_file(
        target,
        REPO_ROOT / "docker-compose.production.yml",
        f"{target.compose_dir}/docker-compose.yml",
        execute=execute,
    )
    write_remote_file(
        target,
        REPO_ROOT / "deploy" / "overrides" / f"{target.name}.compose.yml",
        f"{target.compose_dir}/compose.override.yml",
        execute=execute,
    )
    write_remote_file(
        target,
        REPO_ROOT / "docker" / "rebuild_sandbox_image.sh",
        f"{target.compose_dir}/rebuild_sandbox_image.sh",
        execute=execute,
    )


def compose_command(target: TargetConfig, *arguments: str) -> str:
    command = [
        "docker",
        "compose",
        "--project-name",
        target.compose_project,
        "--env-file",
        target.env_file,
        "-f",
        target.compose_files[0],
        "-f",
        target.compose_files[1],
        *arguments,
    ]
    return " ".join(_shell_quote(item) for item in command)


def _render_env_update_script(
    target: TargetConfig, tags: dict[str, str]
) -> str:
    environment = {
        "REGISTRY_PREFIX": target.registry_prefix,
        "BACKEND_IMAGE_TAG": tags["backend"],
        "FRONTEND_IMAGE_TAG": tags["frontend"],
        "SKILL_IMAGE_TAG": tags["skill"],
        "TASKPILOT_DATA_ROOT": target.data_root,
        "IMAGE_TAG": tags["backend"],
    }
    assignments = " ".join(
        f"{key}={_shell_quote(value)}" for key, value in environment.items()
    )
    return f"""set -euo pipefail
{assignments} python3 - {_shell_quote(target.env_file)} <<'PY'
import os
from pathlib import Path
import stat

updates = {{
    "REGISTRY_PREFIX": os.environ["REGISTRY_PREFIX"],
    "BACKEND_IMAGE_TAG": os.environ["BACKEND_IMAGE_TAG"],
    "FRONTEND_IMAGE_TAG": os.environ["FRONTEND_IMAGE_TAG"],
    "SKILL_IMAGE_TAG": os.environ["SKILL_IMAGE_TAG"],
    "TASKPILOT_DATA_ROOT": os.environ["TASKPILOT_DATA_ROOT"],
    "IMAGE_TAG": os.environ["IMAGE_TAG"],
}}
path = Path(os.sys.argv[1])
if not path.is_file():
    raise SystemExit(f"missing environment file: {{path}}")
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
output = []
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        output.append(line)
        continue
    key = stripped.split("=", 1)[0]
    if key in updates:
        output.append(f"{{key}}={{updates[key]}}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{{key}}={{value}}")
temporary = path.with_name(path.name + ".tmp")
temporary.write_text("\\n".join(output) + "\\n", encoding="utf-8")
os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
os.replace(temporary, path)
PY"""


def _render_state_update_script(
    target: TargetConfig,
    tags: dict[str, str],
    commit: str | None,
    deployed_at: str,
    backup_env: str,
) -> str:
    refs = {
        image_type: image_ref(
            target.registry_prefix, image_type, tags[image_type]
        )
        for image_type in IMAGE_TYPES
    }
    current = {
        "deployed_at": deployed_at,
        "commit": commit,
        "images": {
            image_type: {
                "repository": (
                    f"{target.registry_prefix}/{IMAGE_REPOSITORIES[image_type]}"
                ),
                "tag": tags[image_type],
                "ref": refs[image_type],
            }
            for image_type in IMAGE_TYPES
        },
        "compose_files": list(target.compose_files),
        "data_root": target.data_root,
    }
    state_json = json.dumps(
        {
            "schema_version": 1,
            "target": target.name,
            "current": current,
        },
        ensure_ascii=False,
    )
    environment = {
        "STATE_JSON": state_json,
        "PREVIOUS_ENV_FILE": backup_env,
    }
    assignments = " ".join(
        f"{key}={_shell_quote(value)}" for key, value in environment.items()
    )
    return f"""set -euo pipefail
{assignments} python3 - {_shell_quote(target.state_file)} <<'PY'
import json
import os
from pathlib import Path
import stat

path = Path(os.sys.argv[1])
new_state = json.loads(os.environ["STATE_JSON"])
try:
    old_state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
except json.JSONDecodeError:
    old_state = None

previous = None
if isinstance(old_state, dict) and isinstance(old_state.get("current"), dict):
    previous = old_state["current"]
else:
    tags = {{}}
    backup = Path(os.environ["PREVIOUS_ENV_FILE"])
    if backup.is_file():
        for line in backup.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.strip().split("=", 1)
            if key in ("BACKEND_IMAGE_TAG", "FRONTEND_IMAGE_TAG", "SKILL_IMAGE_TAG"):
                tags[key.removesuffix("_IMAGE_TAG").lower()] = value
    if len(tags) == 3:
        previous = {{"images": {{key: {{"tag": value}} for key, value in tags.items()}}}}

if previous is not None:
    new_state["previous"] = previous
temporary = path.with_name(path.name + ".tmp")
temporary.parent.mkdir(parents=True, exist_ok=True)
temporary.write_text(json.dumps(new_state, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
if path.exists():
    os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
else:
    os.chmod(temporary, 0o640)
os.replace(temporary, path)
PY"""


def deploy_images(
    target: TargetConfig,
    tags: dict[str, str],
    *,
    commit: str | None,
    execute: bool,
    write_local_mirror: bool = False,
) -> None:
    if not target.has_role("deploy"):
        raise ValueError(f"target {target.name} cannot deploy images")
    for image_type in IMAGE_TYPES:
        validate_tag(tags[image_type])

    sync_runtime_files(target, execute=execute)
    deployed_at = datetime.now(timezone.utc).isoformat()
    backup_env_path = (
        Path(target.state_file).parent
        / "backups"
        / f"{deployed_at.replace(':', '')}.env"
    )
    backup_env = backup_env_path.as_posix()
    refs = {
        image_type: image_ref(
            target.registry_prefix, image_type, tags[image_type]
        )
        for image_type in IMAGE_TYPES
    }

    pull_commands = "\n".join(
        f"docker pull {_shell_quote(refs[image_type])}"
        for image_type in IMAGE_TYPES
    )
    script = f"""set -euo pipefail
test -f {_shell_quote(target.env_file)}
    mkdir -p {_shell_quote(backup_env_path.parent.as_posix())}
cp -p {_shell_quote(target.env_file)} {_shell_quote(backup_env)}
{pull_commands}
{_render_env_update_script(target, tags)}
{compose_command(target, 'up', '-d', '--no-build', '--remove-orphans', 'backend', 'frontend')}
for _attempt in $(seq 1 60); do
    if curl -fsS {_shell_quote(target.health_url)} >/dev/null; then
        echo 'health check passed'
        break
    fi
    if [ "$_attempt" -eq 60 ]; then
        echo 'health check timed out' >&2
        exit 1
    fi
    sleep 2
done
{_render_state_update_script(target, tags, commit, deployed_at, backup_env)}
"""
    run_remote(target, script, execute=execute, label="deploy images")

    if execute and write_local_mirror:
        state: dict[str, Any] = {
            "schema_version": 1,
            "target": target.name,
            "current": {
                "deployed_at": deployed_at,
                "commit": commit,
                "images": {
                    image_type: {
                        "tag": tags[image_type],
                        "ref": refs[image_type],
                    }
                    for image_type in IMAGE_TYPES
                },
                "data_root": target.data_root,
            },
        }
        write_local_state(target.name, state)


def read_remote_state(
    target: TargetConfig, *, execute: bool
) -> dict[str, Any] | None:
    state_file = _shell_quote(target.state_file)
    command = f"if [ -f {state_file} ]; then cat {state_file}; else printf ''; fi"
    output = capture_remote(
        target, command, execute=execute, label="read deployment state"
    )
    if not output:
        return None
    state = json.loads(output)
    if not isinstance(state, dict):
        raise ValueError(f"invalid deployment state on {target.name}")
    return state


def rollback_target(
    target: TargetConfig,
    *,
    execute: bool,
    write_local_mirror: bool = False,
) -> None:
    state = read_remote_state(target, execute=execute)
    if not execute:
        print(
            "[dry-run] rollback will read the authoritative state from "
            f"{target.name} and deploy its previous image tags"
        )
        return
    if state is None:
        raise ValueError(f"no deployment state on {target.name}")
    previous = state.get("previous")
    if not isinstance(previous, dict) or not isinstance(previous.get("images"), dict):
        raise ValueError(f"no previous deployment recorded for {target.name}")
    images = previous["images"]
    tags = {}
    for image_type in IMAGE_TYPES:
        value = images.get(image_type)
        if not isinstance(value, dict) or not isinstance(value.get("tag"), str):
            raise ValueError(f"previous {image_type} tag missing for {target.name}")
        tags[image_type] = value["tag"]
    deploy_images(
        target,
        tags,
        commit=previous.get("commit") if isinstance(previous.get("commit"), str) else None,
        execute=execute,
        write_local_mirror=write_local_mirror,
    )
