"""MCP 工具管理 API

权限：增删改/启停/测试 = admin；列表查看 = 全员（绑定的 MCP 工具通过 Agent 间接使用）。
参考 nexent remote_mcp_app 的核心子集（add/update/delete/toggle/test/tools），
社区市场与容器化部分不做。
"""
import json
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.schemas.destroy import DestroyRequest
from app.core.security import verify_password

from app.database import get_db
from app.core.deps import get_current_user, require_admin, require_perm
from app.models.user import User
from app.models.mcp_server import McpServer
from app.services import mcp_service
from app.services.mcp_service import McpService
from app.services import mcp_access, tool_governance
from app.services.mcp_sources import tool_source
from app.services.mcp_presentation import connection_label
from app.services.mcp_credentials import (
    McpCredentialError, McpEncryptionUnavailable, has_inline_credentials,
    public_env, stored_env, update_env,
)

router = APIRouter(prefix="/mcp", tags=["MCP工具"])


class McpServerRequest(BaseModel):
    name: str
    type: str = "stdio"  # stdio | http
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[dict[str, Optional[str]]] = None
    url: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True


class VisibilityUpdate(BaseModel):
    hidden: bool
    roles: list[str] = Field(default_factory=list, max_length=3)
    user_ids: list[int] = Field(default_factory=list, max_length=1000)
    revision: int = Field(ge=0)


def _validate(data: McpServerRequest):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")
    if data.type not in ("stdio", "http"):
        raise HTTPException(status_code=400, detail="type 必须是 stdio 或 http")
    if data.type == "stdio" and not (data.command or "").strip():
        raise HTTPException(status_code=400, detail="stdio 类型必须提供 command")
    if data.type == "http" and not (data.url or "").strip():
        raise HTTPException(status_code=400, detail="http 类型必须提供 url")
    if has_inline_credentials(data.command, data.args or [], data.url):
        raise HTTPException(status_code=400, detail="Place credentials in encrypted MCP environment variables")
    if mcp_service.settings.GOVERNANCE_TOOL_POLICY_ENABLED:
        try:
            mcp_service.validate_destination(McpServer(type=data.type, command=data.command, url=data.url))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


def _to_dict(rec: McpServer, *, include_secrets: bool = True) -> dict:
    try:
        args = json.loads(rec.args_json) if rec.args_json else []
    except Exception:
        args = []
    env = public_env(rec.env_json, rec.env_rotated_at) if include_secrets else {}
    return {
        "id": rec.id,
        "name": rec.name,
        "type": rec.type,
        "command": rec.command if include_secrets else None,
        "args": args if include_secrets else [],
        "env": env if include_secrets else {},
        "env_configured": bool(stored_env(rec.env_json)),
        "url": rec.url if include_secrets else None,
        "description": rec.description,
        "display_description": rec.display_description,
        "enabled": rec.enabled,
        "is_deleted": rec.is_deleted,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "connection": connection_label(rec),
        **({"visibility": mcp_access.visibility(rec)} if include_secrets else {}),
    }


@router.get("/servers")
async def list_servers(
    include_deleted: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """MCP 服务列表（全员可见；写操作仅 admin）"""
    management = include_deleted and current_user.role == "admin"
    records = McpService(db).list_servers(include_deleted=management)
    return [_to_dict(record, include_secrets=management) for record in sorted(
        records, key=lambda item: (bool(item.is_deleted), not item.enabled, item.id))
        if management or mcp_access.can_use(record, current_user)]


@router.get("/servers/{mcp_id}/visibility")
def get_visibility(mcp_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    server = db.get(McpServer, mcp_id)
    if not server:
        raise HTTPException(404, "MCP 服务不存在")
    return mcp_access.visibility(server)


@router.put("/servers/{mcp_id}/visibility")
def put_visibility(mcp_id: int, payload: VisibilityUpdate, request: Request,
                   _: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = mcp_access.save_visibility(db, mcp_id, payload)
    request.state.audit_changes = {"permissions": "changed"}
    return result


@router.get("/servers/{mcp_id}/source/{tool_name}")
def get_source(mcp_id: int, tool_name: str, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    server = db.get(McpServer, mcp_id)
    if not server or server.is_deleted:
        raise HTTPException(404, "MCP 服务不存在")
    return tool_source(db, server, tool_name)


@router.post("/servers")
async def create_server(
    data: McpServerRequest,
    request: Request,
    current_user: User = Depends(require_perm("mcp_manage.create")),
    db: Session = Depends(get_db),
):
    _validate(data)
    svc = McpService(db)
    existing = db.query(McpServer).filter(McpServer.name == data.name.strip()).first()
    if existing:
        raise HTTPException(status_code=400, detail="同名 MCP 服务已存在")
    try:
        rec = svc.create(current_user.id, data.model_dump())
    except McpEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except McpCredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.state.audit_changes = {
        "enabled": {"before": None, "after": rec.enabled},
        **({"env": "changed"} if data.env else {}),
    }
    from app.services.mcp_descriptions import enqueue_descriptions
    enqueue_descriptions()
    return _to_dict(rec)


@router.put("/servers/{mcp_id}")
async def update_server(
    mcp_id: int,
    data: McpServerRequest,
    request: Request,
    current_user: User = Depends(require_perm("mcp_manage.update")),
    db: Session = Depends(get_db),
):
    _validate(data)
    before = McpService(db).get(mcp_id)
    previous_enabled = before.enabled if before else None
    try:
        rec = McpService(db).update(mcp_id, data.model_dump(exclude_unset=True))
    except McpEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except McpCredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rec:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    request.state.audit_changes = {
        "enabled": {"before": previous_enabled, "after": rec.enabled},
        **({"env": "changed"} if data.env else {}),
    }
    from app.services.mcp_descriptions import enqueue_descriptions
    enqueue_descriptions()
    return _to_dict(rec)


@router.delete("/servers/{mcp_id}")
async def delete_server(
    mcp_id: int,
    current_user: User = Depends(require_perm("mcp_manage.delete.soft")),
    db: Session = Depends(get_db),
):
    ok = McpService(db).delete(mcp_id)
    if not ok:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    return {"message": "MCP 服务已软删除，可恢复"}


@router.put('/servers/{mcp_id}/restore')
async def restore_server(mcp_id: int, current_user: User = Depends(require_perm('mcp_manage.restore')),
                         db: Session = Depends(get_db)):
    record = McpService(db).restore(mcp_id)
    if record is None:
        raise HTTPException(404, 'MCP 服务不存在')
    return _to_dict(record)


@router.delete('/servers/{mcp_id}/hard')
async def hard_delete_server(mcp_id: int, data: DestroyRequest,
                             current_user: User = Depends(require_perm('mcp_manage.delete.hard')),
                             db: Session = Depends(get_db)):
    administrator = db.query(User).filter(
        (User.account == data.username) | (User.username == data.username)
    ).first()
    if not administrator or not administrator.is_active or administrator.role != 'admin':
        raise HTTPException(403, '该账号不是有效管理员')
    if not verify_password(data.password, administrator.password_hash):
        raise HTTPException(403, '密码错误')
    if not McpService(db).hard_delete(mcp_id):
        raise HTTPException(404, 'MCP 服务不存在')
    return {'message': 'MCP 服务已彻底删除，历史审计保留'}


@router.post("/servers/{mcp_id}/toggle")
async def toggle_server(
    mcp_id: int,
    request: Request,
    current_user: User = Depends(require_perm("mcp_manage.toggle")),
    db: Session = Depends(get_db),
):
    previous = McpService(db).get(mcp_id)
    was_enabled = previous.enabled if previous else None
    rec = McpService(db).toggle(mcp_id)
    if not rec:
        raise HTTPException(status_code=404, detail="MCP 服务不存在")
    request.state.audit_changes = {"enabled": {"before": was_enabled, "after": rec.enabled}}
    return _to_dict(rec)


@router.post("/test")
async def test_connection(
    data: McpServerRequest,
    current_user: User = Depends(require_perm("mcp_manage.test")),
):
    """不落库，仅测试连接与工具枚举"""
    _validate(data)
    try:
        encrypted_env, _ = update_env(None, data.env)
    except McpEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except McpCredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    probe = McpServer(
        name=data.name.strip(),
        type=data.type,
        command=data.command,
        args_json=json.dumps(data.args or [], ensure_ascii=False),
        env_json=encrypted_env,
        url=data.url,
    )
    return await mcp_service.test_connection(probe)


@router.post("/servers/{mcp_id}/test")
async def test_saved_connection(
    mcp_id: int,
    data: McpServerRequest,
    current_user: User = Depends(require_perm("mcp_manage.test")),
    db: Session = Depends(get_db),
):
    _validate(data)
    record = McpService(db).get(mcp_id)
    if not record:
        raise HTTPException(status_code=404, detail="MCP server not found")
    try:
        combined, _ = update_env(record.env_json, data.env)
    except McpEncryptionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except McpCredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    probe = McpServer(
        name=data.name.strip(), type=data.type, command=data.command,
        args_json=json.dumps(data.args or [], ensure_ascii=False),
        env_json=combined, url=data.url,
    )
    return await mcp_service.test_connection(probe)


@router.get("/servers/{mcp_id}/tools")
async def list_tools(
    mcp_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """连接并枚举该 MCP 服务的工具列表"""
    mcp_access.require_visible(db, mcp_id, current_user)
    tools = [item for item in tool_governance.list_catalog(db, mcp_id) if item["available"]]
    return {"ok": True, "tool_count": len(tools),
            "tools": [{"name": item["name"], "description": item["description"]} for item in tools]}
