"""Immutable Agent revisions with session-bound runtime snapshots."""

import copy
import re
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent import Agent
from app.models.agent_governance import AgentPublication, AgentReleaseEvent, AgentVersion, SessionAgentVersion
from app.models.knowledge import KnowledgeBase
from app.models.mcp_server import McpServer
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.models.tool_governance import AgentToolGrant, McpToolCatalog
from app.services.skill_versions import verify_snapshot
from app.services.skill_service import SkillService


CONFIG_FIELDS = ("mcp_servers", "knowledge_bases", "builtin_skills", "max_react_iters")
EDIT_FIELDS = ("name", "description", "agent_type", "system_prompt",
               "model_config_name", "config", "tool_grants", "owner_id", "department_id", "memory_policy")


def validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot.get("name"), str) or not 0 < len(snapshot["name"]) <= 100:
        raise HTTPException(422, "Agent 名称无效")
    if snapshot.get("agent_type") not in ("dialog", "task", "rag"):
        raise HTTPException(422, "Agent 类型不支持")
    if not isinstance(snapshot.get("model_config_name"), str) or not snapshot["model_config_name"]:
        raise HTTPException(422, "默认模型 ID 无效")
    if not isinstance(snapshot.get("config"), dict):
        raise HTTPException(422, "Agent 配置必须为对象")
    config = snapshot["config"]
    if any(field not in CONFIG_FIELDS + ("_skill_versions",) for field in config):
        raise HTTPException(422, "配置中存在未受控字段")
    for field in ("mcp_servers", "knowledge_bases"):
        values = config.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values
        ) or len(values) != len(set(values)):
            raise HTTPException(422, "Agent 资源 ID 列表无效")
    skills = config.get("builtin_skills", [])
    if not isinstance(skills, list) or any(
        not isinstance(value, str) or not value or len(value) > 100 for value in skills
    ):
        raise HTTPException(422, "Agent Skill 标识列表无效")
    max_iters = config.get("max_react_iters")
    if max_iters is not None and (
        not isinstance(max_iters, int) or isinstance(max_iters, bool) or not 1 <= max_iters <= 100
    ):
        raise HTTPException(422, "Agent 最大推理轮数无效")
    versions = config.get("_skill_versions", {})
    if not isinstance(versions, dict) or any(
        not isinstance(skill_id, str) or not skill_id.isdecimal() or
        not isinstance(version_id, str) or not version_id
        for skill_id, version_id in versions.items()
    ):
        raise HTTPException(422, "Agent Skill 版本锁定无效")
    grants = snapshot.get("tool_grants")
    if not isinstance(grants, list) or len(grants) > 200 or any(
        not isinstance(grant, dict) or
        not isinstance(grant.get("server_id"), int) or isinstance(grant.get("server_id"), bool) or
        grant["server_id"] <= 0 or
        not isinstance(grant.get("tool_name"), str) or not grant["tool_name"] or
        not isinstance(grant.get("schema_digest"), str) or
        not re.fullmatch(r"[0-9a-f]{64}", grant["schema_digest"])
        for grant in grants
    ):
        raise HTTPException(422, "Agent Tool 白名单格式无效")


def reconcile_skill_versions(db: Session, config: dict) -> dict:
    result = copy.deepcopy(config)
    previous = result.get('_skill_versions') or {}
    inactive_packages = SkillService(db)._inactive_package_ids()
    versions = {}
    for skill, publication in db.query(Skill, SkillPublication).join(
        SkillPublication, SkillPublication.skill_id == Skill.id
    ).filter(Skill.is_active.is_(True)).all():
        if skill.package_id in inactive_packages:
            continue
        previous_id = previous.get(str(skill.id))
        existing = db.get(SkillVersion, previous_id) if previous_id else None
        versions[str(skill.id)] = (
            existing.id if existing and existing.skill_id == skill.id and
            existing.status == 'published' else publication.version_id
        )
    result['_skill_versions'] = versions
    return result


def required_skills(db: Session, snapshot: dict) -> list[Skill]:
    config = snapshot.get('config') or {}
    builtin_flag = 'builtin_task' if snapshot.get('agent_type') == 'task' else 'builtin_dialog'
    extras = set(config.get('builtin_skills') or [])
    inactive_packages = SkillService(db)._inactive_package_ids()
    from app.services.skill_hub.access import system_only
    selected = [skill for skill in system_only(db.query(Skill), db, Skill).all() if skill.name in extras or (
        skill.is_active and getattr(skill, builtin_flag)
    )]
    if extras - {skill.name for skill in selected} or any(
        not skill.is_active or skill.package_id in inactive_packages for skill in selected
    ):
        raise HTTPException(409, '所选内置技能已停用或不存在，请重新选择')
    return selected


def snapshot_live(db: Session, agent: Agent) -> dict:
    config = {key: copy.deepcopy((agent.config or {})[key]) for key in CONFIG_FIELDS
              if key in (agent.config or {})}
    config = reconcile_skill_versions(db, config)
    grants = db.query(AgentToolGrant).filter_by(agent_id=agent.id).all()
    return {"name": agent.name, "description": agent.description, "agent_type": agent.agent_type,
            "system_prompt": agent.system_prompt, "model_config_name": agent.model_config_name or settings.MODEL_NAME,
            "config": config, "tool_grants": [{"server_id": grant.server_id, "tool_name": grant.tool_name,
                                             "schema_digest": grant.schema_digest} for grant in grants],
            "owner_id": None, "department_id": None, "memory_policy": "stage6_disabled"}


def baseline(db: Session, agent_id: int, actor_id: int | None = None) -> AgentVersion:
    agent = db.get(Agent, agent_id, with_for_update=True)
    if agent is None:
        raise HTTPException(404, "Agent 不存在")
    if db.get(AgentPublication, agent_id) is not None:
        raise HTTPException(409, "已有 Agent 已发布版本，不能重写基线")
    version = AgentVersion(id=str(uuid4()), agent_id=agent_id, number=1, state="published",
                           snapshot=snapshot_live(db, agent), owner_id=None, department_id=None,
                           test_status="legacy_baseline", created_by=actor_id)
    db.add(version)
    db.flush()
    db.add(AgentPublication(agent_id=agent_id, version_id=version.id, revision=1))
    db.add(AgentReleaseEvent(agent_id=agent_id, version_id=version.id,
                             action="baseline", actor_id=actor_id))
    db.commit()
    return version


def clone(db: Session, agent_id: int, source_id: str, actor_id: int) -> AgentVersion:
    agent = db.get(Agent, agent_id, with_for_update=True)
    source = db.get(AgentVersion, source_id)
    if agent is None or source is None or source.agent_id != agent_id:
        raise HTTPException(404, "Agent 版本不存在")
    highest = db.query(AgentVersion.number).filter_by(agent_id=agent_id).order_by(
        AgentVersion.number.desc()).first()
    version = AgentVersion(id=str(uuid4()), agent_id=agent_id, number=highest[0] + 1,
                           state="draft", snapshot=copy.deepcopy(source.snapshot),
                           owner_id=source.owner_id, department_id=source.department_id,
                           created_by=actor_id)
    db.add(version)
    db.commit()
    return version


def save_draft(db: Session, agent_id: int, version_id: str, revision: int,
               changes: dict) -> AgentVersion:
    version = db.get(AgentVersion, version_id, with_for_update=True)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(404, "Agent 草稿不存在")
    if version.state != "draft" or version.revision != revision:
        raise HTTPException(409, "Agent 草稿已发布或发生并发修改")
    if any(key not in EDIT_FIELDS for key in changes):
        raise HTTPException(422, "不允许修改版本控制字段")
    snapshot = copy.deepcopy(version.snapshot)
    for key, value in changes.items():
        if key == "config":
            if not isinstance(value, dict) or any(field not in CONFIG_FIELDS + ("_skill_versions",)
                                                  for field in value):
                raise HTTPException(422, "配置中存在未受控字段")
            snapshot[key] = copy.deepcopy(value)
        else:
            snapshot[key] = value
    validate_snapshot(snapshot)
    snapshot['config'] = reconcile_skill_versions(db, snapshot['config'])
    if snapshot.get("memory_policy") not in ('stage6_disabled', 'candidate_review', 'disabled') or (
        snapshot.get('memory_policy') == 'candidate_review' and not settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED
    ):
        raise HTTPException(409, "记忆治理阶段未完成，不能开放长期写回")
    version.snapshot = snapshot
    version.owner_id = snapshot.get("owner_id")
    version.department_id = snapshot.get("department_id")
    version.revision += 1
    version.test_status = "untested"
    version.test_summary = None
    db.commit()
    return version


def preflight(db: Session, version: AgentVersion, catalog: list[str]) -> dict:
    snapshot = version.snapshot
    validate_snapshot(snapshot)
    if snapshot.get("model_config_name") not in catalog:
        raise HTTPException(409, "Agent 默认模型不在当前网关聊天模型目录中")
    if snapshot.get("agent_type") not in ("dialog", "task", "rag") or (
        snapshot.get("memory_policy") not in ('stage6_disabled', 'candidate_review', 'disabled') or
        snapshot.get('memory_policy') == 'candidate_review' and not settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED
    ):
        raise HTTPException(409, "Agent 类型或记忆策略尚不可发布")
    config = snapshot.get("config") or {}
    ids = config.get("_skill_versions") or {}
    for skill in required_skills(db, snapshot):
        publication = db.get(SkillPublication, skill.id)
        pinned_id = publication.version_id if publication else None
        item = db.get(SkillVersion, pinned_id) if pinned_id else None
        if item is None or (
            item.skill_id != skill.id or item.status != "published"
        ):
            raise HTTPException(409, "Agent 引用的 Skill 版本缺失或停用")
        verify_snapshot(item)
    for kb_id in config.get("knowledge_bases") or []:
        knowledge = db.get(KnowledgeBase, int(kb_id))
        if knowledge is None or knowledge.scope != "shared":
            raise HTTPException(409, "Agent 知识库不存在或不属于共享范围")
    bound = {int(value) for value in config.get("mcp_servers") or []}
    for server_id in sorted(bound):
        server = db.get(McpServer, server_id)
        if server is None or server.is_deleted:
            raise HTTPException(409, f'MCP 服务 #{server_id} 已删除或不存在，请在资源绑定中移除后保存')
    grants = snapshot.get("tool_grants") or []
    if len(grants) != len({(item["server_id"], item["tool_name"]) for item in grants}):
        raise HTTPException(409, "Agent Tool 快照中存在重复授权")
    for item in grants:
        server_id = item["server_id"]
        server = db.get(McpServer, server_id)
        tool = db.query(McpToolCatalog).filter_by(
            server_id=server_id, name=item["tool_name"]
        ).one_or_none()
        reason = None
        if server_id not in bound:
            reason = '工具所属服务未绑定，请重新选择工具'
        elif tool is None or not tool.available:
            reason = '工具不存在或已不可用，请在 MCP 工具页面刷新目录后重新选择'
        elif tool.risk == 'unknown':
            reason = '工具尚未审核，请在 MCP 工具页面完成审核后重试'
        elif tool.schema_digest != item['schema_digest']:
            reason = '工具参数结构已变化，请重新勾选工具并保存授权'
        if reason:
            name = server.name if server else f'服务 #{server_id}'
            raise HTTPException(409, f'MCP「{name}」工具「{item["tool_name"]}」：{reason}')
    users = db.query(Agent).filter_by(id=version.agent_id).count()
    return {"model": snapshot["model_config_name"], "skill_versions": len(ids),
            "tool_grants": len(grants), "agent_present": bool(users)}


def diff(first: AgentVersion, second: AgentVersion) -> dict:
    return {key: {"before": first.snapshot.get(key), "after": second.snapshot.get(key)}
            for key in sorted(first.snapshot.keys() | second.snapshot.keys())
            if first.snapshot.get(key) != second.snapshot.get(key)}


def publish(db: Session, agent_id: int, version_id: str, revision: int,
            actor_id: int, catalog: list[str], *, rollback: bool = False) -> AgentPublication:
    agent = db.get(Agent, agent_id, with_for_update=True)
    publication = db.get(AgentPublication, agent_id, with_for_update=True)
    version = db.get(AgentVersion, version_id, with_for_update=True)
    if agent is None or version is None or version.agent_id != agent_id:
        raise HTTPException(404, "Agent 或版本不存在")
    if (publication.revision if publication else 0) != revision or (
        publication is not None and publication.version_id == version_id
    ):
        raise HTTPException(409, "Agent 发布指针已变化或已是目标版本")
    if (rollback and version.state != "published") or (not rollback and (
        version.state != "draft" or version.test_status != "passed"
    )):
        raise HTTPException(409, "仅通过测试的草稿或已发布历史版可切流")
    preflight(db, version, catalog)
    previous = publication.version_id if publication else None
    snapshot = version.snapshot
    agent.name = snapshot["name"]
    agent.description = snapshot.get("description")
    agent.agent_type = snapshot["agent_type"]
    agent.model_config_name = snapshot["model_config_name"]
    agent.system_prompt = snapshot.get("system_prompt")
    agent.is_active = True
    agent.config = {key: copy.deepcopy(value) for key, value in snapshot["config"].items()
                    if key in CONFIG_FIELDS + ("_skill_versions",)}
    db.query(AgentToolGrant).filter_by(agent_id=agent_id).delete(synchronize_session=False)
    db.add_all([AgentToolGrant(agent_id=agent_id, server_id=item["server_id"],
                               tool_name=item["tool_name"], schema_digest=item["schema_digest"],
                               granted_by=actor_id) for item in snapshot.get("tool_grants") or []])
    version.state = "published"
    if publication is None:
        publication = AgentPublication(agent_id=agent_id, version_id=version.id, revision=1)
        db.add(publication)
    else:
        publication.version_id = version.id
        publication.revision += 1
    db.add(AgentReleaseEvent(agent_id=agent_id, previous_version_id=previous,
                             version_id=version.id, action="rollback" if rollback else "publish",
                             actor_id=actor_id))
    db.commit()
    from app.agents.manager import agent_manager
    from app.core.cache import cache_delete_pattern
    agent_manager.clear_cache()
    cache_delete_pattern("agents:*")
    return publication


def for_session(db: Session, agent: Agent, session_id: int) -> SimpleNamespace:
    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        return agent
    pin = db.get(SessionAgentVersion, session_id)
    if pin is not None and pin.agent_id != agent.id:
        raise HTTPException(409, "会话 Agent 版本与 Agent 不符")
    if pin is None:
        raise HTTPException(409, '历史会话未固定 Agent 基线版本，请先运行会话版本回填')
    version = db.get(AgentVersion, pin.version_id)
    if version is None or version.agent_id != agent.id or version.state != "published":
        raise HTTPException(409, "会话固定的 Agent 版本已失效")
    snapshot = version.snapshot
    return SimpleNamespace(id=agent.id, name=snapshot["name"],
                           description=snapshot.get("description"), agent_type=snapshot["agent_type"],
                           model_config_name=snapshot["model_config_name"],
                           system_prompt=snapshot.get("system_prompt"),
                           is_active=agent.is_active,
                           config={**copy.deepcopy(snapshot["config"]),
                                   "_agent_version_id": version.id,
                                   "_memory_policy": snapshot.get('memory_policy', 'stage6_disabled'),
                                   "_tool_grants": copy.deepcopy(snapshot.get("tool_grants") or [])})
