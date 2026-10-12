"""Deployment inventory loading and target validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DEPLOY_ROOT.parent


class ConfigError(ValueError):
    """Raised when deployment configuration is invalid."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class TargetConfig:
    name: str
    host: str
    ssh_user: str
    ssh_port: int
    roles: tuple[str, ...]
    registry_prefix: str
    repo_path: str | None
    compose_dir: str
    data_root: str
    compose_project: str
    env_file: str
    state_file: str
    backend_container: str
    frontend_container: str
    health_url: str

    @property
    def ssh_destination(self) -> str:
        return f"{self.ssh_user}@{self.host}"

    @property
    def compose_files(self) -> tuple[str, str]:
        return (
            f"{self.compose_dir}/docker-compose.yml",
            f"{self.compose_dir}/compose.override.yml",
        )

    def has_role(self, role: str) -> bool:
        return role in self.roles


def target_names() -> list[str]:
    inventory = _read_json(DEPLOY_ROOT / "inventory.yml")
    return list(inventory.get("targets", {}))


def parse_target_selection(selection: str) -> list[str]:
    names = target_names()
    if selection.strip().lower() == "all":
        return names
    selected = []
    for item in selection.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in names:
            raise ConfigError(
                f"unknown target {name!r}; valid targets: {', '.join(names)}"
            )
        if name not in selected:
            selected.append(name)
    if not selected:
        raise ConfigError("at least one target is required")
    return selected


def load_target(name: str) -> TargetConfig:
    inventory = _read_json(DEPLOY_ROOT / "inventory.yml")
    targets = inventory.get("targets", {})
    if name not in targets:
        raise ConfigError(
            f"unknown target {name!r}; valid targets: {', '.join(targets)}"
        )

    target_path = Path(targets[name])
    if not target_path.is_absolute():
        target_path = DEPLOY_ROOT / target_path
    defaults = {
        "registry": inventory.get("registry", {}),
        "ssh": inventory.get("ssh", {}),
        **inventory.get("common", {}),
    }
    target = _deep_merge(defaults, _read_json(target_path))

    host = target.get("host")
    ssh = target.get("ssh", {})
    registry = target.get("registry", {})
    paths = target.get("paths", {})
    compose = target.get("compose", {})
    health = target.get("health", {})
    required = (
        ("host", host),
        ("registry.prefix", registry.get("prefix")),
        ("paths.compose_dir", paths.get("compose_dir")),
        ("paths.data_root", paths.get("data_root")),
        ("compose.project", compose.get("project")),
        ("compose.env_file", compose.get("env_file")),
        ("compose.state_file", compose.get("state_file")),
        ("compose.backend_container", compose.get("backend_container")),
        ("health.url", health.get("url")),
    )
    for label, value in required:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"target {name}: {label} is required")

    roles = target.get("roles", [])
    if not isinstance(roles, list) or not all(
        isinstance(role, str) for role in roles
    ):
        raise ConfigError(f"target {name}: roles must be a list of strings")

    repo_path = paths.get("repo")
    if repo_path is not None and not isinstance(repo_path, str):
        raise ConfigError(f"target {name}: paths.repo must be a string")

    return TargetConfig(
        name=name,
        host=host,
        ssh_user=str(ssh.get("user", "root")),
        ssh_port=int(ssh.get("port", 22)),
        roles=tuple(roles),
        registry_prefix=registry["prefix"],
        repo_path=repo_path,
        compose_dir=paths["compose_dir"],
        data_root=paths["data_root"],
        compose_project=compose["project"],
        env_file=compose["env_file"],
        state_file=compose["state_file"],
        backend_container=compose["backend_container"],
        frontend_container=compose.get("frontend_container", ""),
        health_url=health["url"],
    )
