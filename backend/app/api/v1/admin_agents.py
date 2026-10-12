"""Agent draft editing and controlled release, separate from legacy user routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, require_admin
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_governance import AgentPublication, AgentReleaseEvent, AgentVersion
from app.models.user import User
from app.services import agent_versions
from app.services.agent_draft_probe import run_isolated_agent
from app.services.model_access import available_catalog
from app.services.model_governance import bounded_test
from app.schemas.agent import CreateAgentRequest


router = APIRouter(prefix="/admin/agents", tags=["Agent 版本治理"])


@router.get("/versioning")
def versioning(_: User = Depends(get_current_user)):
    return {"enabled": settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED}


class DraftCreate(BaseModel):
    source_id: str


class DraftChange(BaseModel):
    revision: int = Field(ge=0)
    changes: dict[str, Any] = Field(default_factory=dict)


class ReleaseRequest(BaseModel):
    revision: int = Field(ge=0)


class TestRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=200)


def describe(version: AgentVersion) -> dict:
    return {"id": version.id, "agent_id": version.agent_id, "number": version.number,
            "state": version.state, "snapshot": version.snapshot, "revision": version.revision,
            "test_status": version.test_status, "test_summary": version.test_summary,
            "created_at": version.created_at}


@router.post("/")
def create_agent(data: CreateAgentRequest, request: Request,
                 actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        raise HTTPException(409, "请先完成版本迁移后再启用 Agent 草稿创建")
    if data.agent_type not in ("dialog", "task", "rag"):
        raise HTTPException(422, "Agent 类型不支持")
    if data.config and any(key not in agent_versions.CONFIG_FIELDS for key in data.config):
        raise HTTPException(422, "配置中存在未受控字段")
    agent = Agent(name=data.name, description=data.description, agent_type=data.agent_type,
                  model_config_name=data.model_config_name or settings.MODEL_NAME,
                  system_prompt=data.system_prompt, config=data.config or {}, is_active=False)
    db.add(agent)
    db.flush()
    from uuid import uuid4
    version = AgentVersion(id=str(uuid4()), agent_id=agent.id, number=1,
                           state="draft", snapshot=agent_versions.snapshot_live(db, agent),
                           created_by=actor.id)
    db.add(version)
    db.commit()
    request.state.audit_changes = {"agent_draft": "created"}
    return describe(version)


@router.get("/{agent_id}/versions")
def versions(agent_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.get(Agent, agent_id) is None:
        raise HTTPException(404, "Agent 不存在")
    published = db.get(AgentPublication, agent_id)
    rows = db.query(AgentVersion).filter_by(agent_id=agent_id).order_by(
        AgentVersion.number.desc()).all()
    events = db.query(AgentReleaseEvent).filter_by(agent_id=agent_id).order_by(
        AgentReleaseEvent.id.desc()).limit(50).all()
    return {"agent_id": agent_id, "published_version_id": published.version_id if published else None,
            "versioning_enabled": settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED,
            "revision": published.revision if published else 0,
            "versions": [describe(row) for row in rows],
            "history": [{"id": event.id, "action": event.action,
                         "previous_version_id": event.previous_version_id,
                         "version_id": event.version_id, "created_at": event.created_at}
                        for event in events]}


@router.post("/{agent_id}/versions")
def clone(agent_id: int, payload: DraftCreate, request: Request,
          actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = agent_versions.clone(db, agent_id, payload.source_id, actor.id)
    request.state.audit_changes = {"agent_draft": "created"}
    return describe(version)


@router.put("/{agent_id}/versions/{version_id}")
def save(agent_id: int, version_id: str, payload: DraftChange, request: Request,
         _: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = agent_versions.save_draft(db, agent_id, version_id, payload.revision,
                                        payload.changes)
    request.state.audit_changes = {"agent_draft": "revised"}
    return describe(version)


@router.get("/{agent_id}/versions/{version_id}/diff")
def diff(agent_id: int, version_id: str, _: User = Depends(require_admin),
         db: Session = Depends(get_db)):
    target = db.get(AgentVersion, version_id)
    published = db.get(AgentPublication, agent_id)
    if target is None or target.agent_id != agent_id or published is None:
        raise HTTPException(404, "Agent 版本或发布记录不存在")
    current = db.get(AgentVersion, published.version_id)
    return {"from": current.id, "to": target.id,
            "fields": agent_versions.diff(current, target)}


@router.post("/{agent_id}/versions/{version_id}/test")
async def test_version(agent_id: int, version_id: str, payload: TestRequest,
                       request: Request, actor: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    version = db.get(AgentVersion, version_id)
    if version is None or version.agent_id != agent_id or version.state != "draft":
        raise HTTPException(404, "Agent 草稿不存在")
    revision = version.revision
    version.test_status = "testing"
    version.test_summary = None
    db.commit()
    try:
        catalog = await available_catalog()
        evidence = agent_versions.preflight(db, version, catalog)
        result = await bounded_test(db, actor, evidence["model"],
                                    'Reply with OK. Do not use tools.', 16, require_price=False)
        isolated_ok = await run_isolated_agent(agent_id, version_id, revision)
        if not isolated_ok:
            raise HTTPException(409, "合成 Agent 构建或回复失败，不允许发布")
        db.refresh(version)
        if version.revision != revision or version.state != "draft":
            raise HTTPException(409, "测试期间草稿已变化，请重新测试")
        config = version.snapshot["config"]
        unsupported = version.snapshot["agent_type"] not in ("dialog", "task")
        version.test_status = "partial_passed" if unsupported else "passed"
        version.test_summary = ("存在未隔离验证的 MCP、知识库或 Agent 架构依赖，不允许发布"
                                if unsupported else "模型在线探测、冻结 Skill 构建与合成 MCP/知识库链路通过（无业务副作用）")
        db.commit()
        request.state.audit_changes = {"agent_test": version.test_status}
        return {"evidence": evidence, "test_status": version.test_status,
                "model_test": result, "warning": version.test_summary}
    except Exception as error:
        db.refresh(version)
        if version.revision == revision and version.state == "draft":
            version.test_status = "failed"
            detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else '配置检查未通过，请重试或联系管理员'
            version.test_summary = f'{detail}；未修改线上配置'
            db.commit()
        raise


@router.post("/{agent_id}/versions/{version_id}/publish")
async def publish(agent_id: int, version_id: str, payload: ReleaseRequest,
                  request: Request, actor: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    publication = agent_versions.publish(db, agent_id, version_id,
        payload.revision, actor.id, await available_catalog())
    request.state.audit_changes = {"agent_version": "published"}
    return {"version_id": publication.version_id, "revision": publication.revision}


@router.post("/{agent_id}/versions/{version_id}/rollback")
async def rollback(agent_id: int, version_id: str, payload: ReleaseRequest,
                   request: Request, actor: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    publication = agent_versions.publish(db, agent_id, version_id,
        payload.revision, actor.id, await available_catalog(), rollback=True)
    request.state.audit_changes = {"agent_version": "rolled_back"}
    return {"version_id": publication.version_id, "revision": publication.revision}
