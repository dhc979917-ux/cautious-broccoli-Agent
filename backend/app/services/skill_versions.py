"""Content-addressed Skill snapshots in the persisted knowledge mount."""

import hashlib
import json
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillReleaseEvent, SkillVersion
from app.services.skill_service import SkillService
from app.services.isolated_skill_runner import run_sandboxed
from app.core.roles import ROLE_LEVELS


MAX_FILES = 2000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
SANDBOX_BOOTSTRAP = (
    "import hashlib, io, json, pathlib, runpy, sys, types\n"
    "root = pathlib.Path('/skill')\n"
    "manifest = json.loads(sys.argv[1])\n"
    "paths = {entry['path'] for entry in manifest}\n"
    "assert len(paths) == len(manifest) and 'SKILL.md' in paths\n"
    "entries = list(root.rglob('*'))\n"
    "assert not any(path.is_symlink() for path in entries)\n"
    "assert {path.relative_to(root).as_posix() for path in entries if path.is_file()} == paths\n"
    "for entry in manifest:\n"
    "    path = root / entry['path']\n"
    "    data = path.read_bytes()\n"
    "    assert len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256']\n"
    "dependencies = json.loads(sys.argv[4])\n"
    "expected = {entry['key'] for entry in dependencies}\n"
    "assert len(expected) == len(dependencies)\n"
    "visited = set()\n"
    "def simulate(key):\n"
    "    if key not in expected:\n"
    "        raise ValueError('undeclared simulated dependency')\n"
    "    visited.add(key)\n"
    "    return {'simulated': True, 'fixture': 'synthetic'}\n"
    "module = types.ModuleType('skill_dependency_mocks')\n"
    "module.model = lambda model_id, prompt='': simulate('model:' + str(model_id))\n"
    "module.tool = lambda server_id, name, arguments=None: simulate('tool:' + str(server_id) + ':' + str(name))\n"
    "module.skill = lambda skill_id, version_id: simulate('skill:' + str(skill_id) + ':' + str(version_id))\n"
    "module.declared = sorted(expected)\n"
    "module.invoke = simulate\n"
    "sys.modules[module.__name__] = module\n"
    "script = sys.argv[2]\n"
    "if script:\n"
    "    assert script in paths and script.endswith('.py')\n"
    "    sys.stdin = io.StringIO(sys.argv[3])\n"
    "    sys.argv = [str(root / script)]\n"
    "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
    "    print('SKILL_TEST_MOCKS:' + json.dumps(sorted(visited)))\n"
    "else:\n"
    "    assert (root / 'SKILL.md').read_text(encoding='utf-8').strip()\n"
    "    print('isolated smoke passed')\n"
)


def storage_root() -> Path:
    return Path(settings.KNOWLEDGE_DIR).resolve() / "skill_versions"


def source_dir(skill: Skill, agents_dir: Path | None = None) -> Path:
    relative = Path(skill.rel_path or "")
    if not relative.parts or relative.parts[0] not in ("skills", "skills_dialog", "skills_task") or (
        any(part in (".", "..") for part in relative.parts)
    ):
        raise HTTPException(409, "Skill 原目录不可确认，无法生成版本")
    base = (agents_dir or Path(SkillService._agents_dir())).resolve()
    candidate = base.joinpath(*relative.parts)
    if any(part.is_symlink() for part in (candidate, *candidate.parents[:len(relative.parts) - 1])):
        raise HTTPException(409, "Skill 原目录包含符号链接")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or not (resolved / "SKILL.md").is_file():
        raise HTTPException(409, "Skill 原目录缺失，已发布版本仍保留")
    return resolved


def _files(root: Path) -> list[Path]:
    files = []
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HTTPException(409, "Skill 包含符号链接，不能冻结为快照")
        if path.is_file():
            relative = path.relative_to(root)
            if any(part.startswith(".staging-") for part in relative.parts):
                raise HTTPException(409, "Skill 路径包含受限暂存文件")
            files.append(path)
            total += path.stat().st_size
            if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise HTTPException(413, "Skill 文件数或大小超过快照限制")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _hash_manifest(manifest: list[dict]) -> str:
    payload = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_snapshot(db: Session, skill_id: int, actor_id: int | None = None, *,
                    agents_dir: Path | None = None, root: Path | None = None,
                    baseline: bool = False, upload_source: Path | None = None,
                    commit: bool = True) -> SkillVersion:
    skill = db.get(Skill, skill_id, with_for_update=True)
    if skill is None:
        raise HTTPException(404, "Skill 不存在")
    publication = db.get(SkillPublication, skill_id)
    if baseline and (publication is not None or db.query(SkillVersion.id).filter_by(skill_id=skill_id).first()):
        raise HTTPException(409, "已有版本，不能覆盖历史基线")
    if Path(skill.name).name != skill.name or skill.name in (".", ".."):
        raise HTTPException(409, "Skill 名称不是安全的目录名")
    source = upload_source if upload_source is not None else source_dir(skill, agents_dir)
    version_id = str(uuid4())
    archive_root = (root or storage_root()).resolve()
    final = archive_root / str(skill_id) / version_id / skill.name
    staging = archive_root / str(skill_id) / f".staging-{version_id}" / skill.name
    staging.mkdir(parents=True, exist_ok=False)
    manifest = []
    try:
        for path in _files(source):
            relative = path.relative_to(source)
            current = path.stat()
            data = path.read_bytes()
            if path.stat().st_size != current.st_size or path.stat().st_mtime_ns != current.st_mtime_ns:
                raise HTTPException(409, "Skill 文件在复制期间发生变化，请重试")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            manifest.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                             "bytes": len(data)})
        if not any(item["path"] == "SKILL.md" for item in manifest):
            raise HTTPException(409, "Skill 缺少 SKILL.md")
        from app.services.skill_resource_files import durable_directory, durable_tree
        durable_tree(staging)
        staging.parent.rename(final.parent)
        durable_directory(final.parent.parent)
    except BaseException:
        shutil.rmtree(staging.parent, ignore_errors=True)
        raise
    version = SkillVersion(id=version_id, skill_id=skill_id,
                           source_rel_path=skill.rel_path,
                           snapshot_rel_path=final.relative_to(archive_root).as_posix(),
                           manifest=manifest, content_hash=_hash_manifest(manifest),
                           status="published" if baseline else "draft",
                           test_status="legacy_baseline" if baseline else "untested",
                           dependencies=[], created_by=actor_id, owner_id=skill.created_by)
    db.add(version)
    db.flush()
    if baseline:
        db.add(SkillPublication(skill_id=skill_id, version_id=version_id, revision=1))
        db.add(SkillReleaseEvent(skill_id=skill_id, version_id=version_id,
                                 action="baseline", actor_id=actor_id))
    if commit:
        db.commit()
    return version


def verify_snapshot(version: SkillVersion, root: Path | None = None) -> Path:
    base = (root or storage_root()).resolve()
    directory = (base / version.snapshot_rel_path).resolve()
    if not directory.is_relative_to(base) or not directory.is_dir():
        raise HTTPException(503, "Skill 快照丢失或存储未挂载")
    if version.content_hash != _hash_manifest(version.manifest):
        raise HTTPException(503, "Skill 版本文件清单不匹配")
    expected_files = {entry["path"] for entry in version.manifest}
    if {path.relative_to(directory).as_posix() for path in _files(directory)} != expected_files:
        raise HTTPException(503, "Skill 快照文件清单发生变化")
    for entry in version.manifest:
        relative = Path(entry["path"])
        path = directory / relative
        if not relative.parts or any(part in (".", "..") for part in relative.parts) or (
            path.is_symlink() or not path.resolve().is_relative_to(directory) or not path.is_file()
        ):
            raise HTTPException(503, "Skill 快照路径不可信")
        if path.stat().st_size != entry["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise HTTPException(503, "Skill 快照内容被修改")
    return directory


def published_dir(db: Session, skill_id: int) -> Path:
    published = db.get(SkillPublication, skill_id)
    if published is None:
        raise HTTPException(409, "Skill 未建立已发布版本")
    version = db.get(SkillVersion, published.version_id)
    if version is None or version.status != "published":
        raise HTTPException(409, "Skill 当前发布版无效")
    return verify_snapshot(version)


def validate_dependencies(db: Session, version: SkillVersion, allowed_drafts=None) -> None:
    allowed_drafts = set(allowed_drafts or [])
    from app.models.mcp_server import McpServer
    from app.models.tool_governance import McpToolCatalog
    from app.models.model_governance import ModelMetadata

    seen = set()

    def visit(current: SkillVersion, ancestors: set[str]) -> None:
        if current.id in ancestors:
            raise HTTPException(409, "Skill 依赖存在环路")
        for dependency in current.dependencies or []:
            kind = dependency.get("kind")
            if kind == "skill":
                skill_id = dependency.get("skill_id")
                version_id = dependency.get("version_id")
                key = (kind, skill_id)
                target = db.get(SkillVersion, version_id)
                skill = db.get(Skill, skill_id)
                if skill is None or not skill.is_active or target is None or (
                    target.skill_id != skill_id or (target.status != "published" and target.id not in allowed_drafts)
                ):
                    raise HTTPException(409, "Skill 依赖版本缺失或已停用")
                verify_snapshot(target)
                visit(target, ancestors | {current.id})
            elif kind == "tool":
                server_id = dependency.get("server_id")
                tool_name = dependency.get("tool_name")
                key = (kind, server_id, tool_name)
                server = db.get(McpServer, server_id)
                tool = db.query(McpToolCatalog).filter_by(server_id=server_id, name=tool_name).one_or_none()
                if server is None or not server.enabled or tool is None or not tool.available or (
                    tool.risk == "unknown" or tool.schema_digest != dependency.get("schema_digest")
                ):
                    raise HTTPException(409, "Tool 依赖尚未审核或 schema 已变化")
            elif kind == "model":
                model_id = dependency.get("model_id")
                key = (kind, model_id)
                metadata = db.get(ModelMetadata, model_id)
                if metadata is None or not metadata.enabled:
                    raise HTTPException(409, "模型依赖不在已启用目录中")
            else:
                raise HTTPException(422, "不支持的 Skill 依赖类型")
            if current.id == version.id:
                if key in seen:
                    raise HTTPException(409, "Skill 依赖重复")
                seen.add(key)

    visit(version, set())


def revise_draft(db: Session, skill_id: int, version_id: str, revision: int, *,
                 dependencies: list[dict], category: str | None, tags: list[str],
                 owner_id: int | None) -> SkillVersion:
    version = db.get(SkillVersion, version_id, with_for_update=True)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(404, "Skill 版本不存在")
    if version.status != "draft" or version.revision != revision:
        raise HTTPException(409, "Skill 草稿版本已发布或修订号已变化")
    if len(tags) != len(set(tags)):
        raise HTTPException(422, "Skill 标签重复")
    version.dependencies = dependencies
    version.category = category
    version.tags = tags
    version.owner_id = owner_id
    version.revision += 1
    version.test_status = "untested"
    version.test_summary = None
    validate_dependencies(db, version)
    db.commit()
    return version


def publish(db: Session, skill_id: int, version_id: str, revision: int,
            actor_id: int, *, rollback: bool = False, commit: bool = True,
            notify_package: bool = True, allowed_drafts=None) -> SkillPublication:
    skill = db.get(Skill, skill_id, with_for_update=True)
    if skill is None or not skill.is_active:
        raise HTTPException(409, "Skill 不存在或已停用")
    version = db.get(SkillVersion, version_id, with_for_update=True)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(404, "Skill 版本不存在")
    publication = db.get(SkillPublication, skill_id, with_for_update=True)
    if (publication.revision if publication else 0) != revision:
        raise HTTPException(409, "Skill 发布指针已变化，请重新检查")
    if (rollback and version.status != "published") or (not rollback and (
        version.status != "draft" or not (version.test_status == "passed" or (
            version.test_status == "smoke_passed" and not version.dependencies))
    )):
        raise HTTPException(409, "只有完成隔离测试的草稿或历史发布版可发布")
    validate_dependencies(db, version, allowed_drafts)
    verify_snapshot(version)
    previous = publication.version_id if publication else None
    if previous == version_id:
        raise HTTPException(409, "当前已是该 Skill 版本")
    if commit and notify_package and skill.package_id and previous is not None:
        from app.services.skill_package_versions import ensure_baseline
        from app.models.skill_resource import PackageVersion
        from sqlalchemy import inspect
        if inspect(db.bind).has_table(PackageVersion.__tablename__):
            ensure_baseline(db, skill.package_id, actor_id)
    version.status = "published"
    if publication is None:
        publication = SkillPublication(skill_id=skill_id, version_id=version_id, revision=1)
        db.add(publication)
    else:
        publication.version_id = version_id
        publication.revision += 1
    db.add(SkillReleaseEvent(skill_id=skill_id, previous_version_id=previous,
                             version_id=version_id, actor_id=actor_id,
                             action="rollback" if rollback else "publish"))
    if commit:
        if notify_package and skill.package_id:
            from app.services.skill_package_versions import record_member_publication
            record_member_publication(db, skill.package_id, actor_id)
        db.commit()
        invalidate_runtime()
    return publication


def invalidate_runtime():
    from app.agents.manager import agent_manager
    agent_manager.clear_cache()
    from app.core.cache import cache_delete_pattern
    cache_delete_pattern('skills:*')


def source_changed(db: Session, skill: Skill, agents_dir: Path | None = None) -> bool:
    publication = db.get(SkillPublication, skill.id)
    if publication is None:
        return True
    source = source_dir(skill, agents_dir)
    manifest = [{"path": path.relative_to(source).as_posix(),
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 "bytes": path.stat().st_size} for path in _files(source)]
    current_hash = _hash_manifest(manifest)
    return not db.query(SkillVersion.id).filter_by(
        skill_id=skill.id, content_hash=current_hash
    ).first()


def runtime_dirs(db: Session, agent_model, selected_skills: list[str] | None,
                 user_role: str, builtin_root: str | None) -> tuple[list[tuple[Path, str]], list[Path]]:
    from app.services.skill_runtime import resolve_plan
    plan = getattr(agent_model, '_skill_runtime_plan', None) or resolve_plan(
        db, agent_model, selected_skills, user_role, builtin_root, (agent_model.config or {}).get('_skill_actor_id'))
    return list(plan.optional), list(plan.builtin)


def file_changes(first: SkillVersion, second: SkillVersion) -> list[dict]:
    old = {entry["path"]: entry["sha256"] for entry in first.manifest}
    new = {entry["path"]: entry["sha256"] for entry in second.manifest}
    return [{"path": path, "change": "added" if path not in old else "removed" if path not in new
             else "modified"} for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]


def sandbox_smoke_test(db: Session, skill_id: int, version_id: str,
                       script: str | None = None, sample_input: str = "",
                       *, commit: bool = True, allowed_drafts=None) -> dict:
    version = db.get(SkillVersion, version_id, with_for_update=True)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(404, "Skill 版本不存在")
    if version.status != "draft":
        raise HTTPException(409, "仅草稿可运行隔离测试")
    validate_dependencies(db, version, allowed_drafts)
    source = verify_snapshot(version)
    image = settings.SKILL_TEST_IMAGE.strip()
    if not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image):
        raise HTTPException(503, "未配置固定摘要的隔离测试镜像，不执行 Skill 测试")
    host_root = PurePosixPath(settings.SKILL_TEST_HOST_KNOWLEDGE_DIR.strip())
    if not host_root.is_absolute() or '..' in host_root.parts or len(host_root.parts) < 4:
        raise HTTPException(503, '未配置可信的宿主机知识库挂载路径，不能运行隔离测试')
    relative_snapshot = source.relative_to(storage_root().resolve())
    host_source = host_root / 'skill_versions' / PurePosixPath(*relative_snapshot.parts)
    if script is not None:
        relative = Path(script)
        if not relative.parts or relative.suffix != ".py" or any(
            part in (".", "..") for part in relative.parts
        ) or relative.is_absolute() or not (source / relative).resolve().is_relative_to(source) or (
            not (source / relative).is_file()
        ):
            raise HTTPException(422, "测试脚本不在 Skill 快照中")
        if len(sample_input) > 4000:
            raise HTTPException(422, "虚构测试输入过长")
    started = time.monotonic()
    dependencies = [
        {'key': f"model:{entry['model_id']}"} if entry['kind'] == 'model' else
        {'key': f"tool:{entry['server_id']}:{entry['tool_name']}"} if entry['kind'] == 'tool' else
        {'key': f"skill:{entry['skill_id']}:{entry['version_id']}"}
        for entry in (version.dependencies or [])
    ]
    try:
        result = run_sandboxed(image, host_source, SANDBOX_BOOTSTRAP,
                               [json.dumps(version.manifest, ensure_ascii=False, separators=(',', ':')),
                                relative.as_posix() if script else '', sample_input,
                                json.dumps(dependencies, separators=(',', ':'))])
    except Exception as error:
        version.test_status = "failed"
        version.test_summary = type(error).__name__
        if commit:
            db.commit()
        raise HTTPException(503, "隔离测试环境无法安全运行") from error
    elapsed_ms = int((time.monotonic() - started) * 1000)
    passed = result.returncode == 0 and (bool(script) or "isolated smoke passed" in result.stdout)
    evidence_line = next((line for line in reversed(result.stdout.splitlines())
                          if line.startswith('SKILL_TEST_MOCKS:')), '')
    try:
        visited = json.loads(evidence_line.removeprefix('SKILL_TEST_MOCKS:'))
    except (ValueError, TypeError):
        visited = []
    if not isinstance(visited, list) or any(not isinstance(key, str) for key in visited):
        visited = []
    all_exercised = bool(evidence_line) and set(visited) == {entry['key'] for entry in dependencies}
    version.test_status = ('passed' if all_exercised else
                           'partial_passed' if evidence_line else 'failed') if passed and script else (
        'smoke_passed' if passed else 'failed')
    version.test_summary = (f"隔离{'脚本' if script else '烟测'}，未调用真实模型或工具，{elapsed_ms}ms"
                            + ('；部分模拟依赖未触发，不允许发布' if version.test_status == 'partial_passed' else ''))
    if commit:
        db.commit()
    return {"status": version.test_status, "mode": "isolated_mock" if dependencies else (
                "isolated_script" if script else "isolated_smoke"),
            "elapsed_ms": elapsed_ms, "model_tokens": 0, "tool_calls": [],
            "simulated_dependencies": visited,
            "warning": "仅调用沙箱内模拟依赖；没有实际模型 Token 消耗或 MCP 工具执行"}
