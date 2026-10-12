"""One MCP visibility policy for directories, Agent binding and execution."""

from fastapi import HTTPException

from app.models.mcp_server import McpServer
from app.models.user import User


ROLES = {"viewer", "operator", "admin"}


def can_use(server, user):
    return bool(server and user and user.is_active and server.enabled and not server.is_deleted
                and (not server.hidden or user.role in (server.visibility_roles or [])
                     or user.id in (server.visibility_user_ids or [])))


def allowed_ids(db, user, ids=None):
    if not user or not user.is_active or ids == []:
        return []
    query = db.query(McpServer).filter(McpServer.enabled.is_(True), McpServer.is_deleted.is_(False))
    if ids is not None:
        query = query.filter(McpServer.id.in_(ids))
    return [server.id for server in query.order_by(McpServer.id) if can_use(server, user)]


def require_visible(db, server_id, user):
    server = db.get(McpServer, server_id)
    if not can_use(server, user):
        raise HTTPException(404, "MCP 服务不存在或无权访问")
    return server


def visibility(server):
    return {"hidden": bool(server.hidden), "roles": server.visibility_roles or [],
            "user_ids": server.visibility_user_ids or [], "revision": server.visibility_revision or 0}


def save_visibility(db, server_id, payload):
    server = db.query(McpServer).filter_by(id=server_id).with_for_update().one_or_none()
    if server is None or server.is_deleted:
        raise HTTPException(404, "MCP 服务不存在")
    if (server.visibility_revision or 0) != payload.revision:
        raise HTTPException(409, "可见范围已变更，请重新打开后保存")
    if not set(payload.roles) <= ROLES:
        raise HTTPException(422, "未知角色")
    ids = sorted(set(payload.user_ids))
    if ids and db.query(User).filter(User.id.in_(ids), User.is_active.is_(True)).count() != len(ids):
        raise HTTPException(422, "所选人员不存在或已停用")
    server.hidden = payload.hidden
    server.visibility_roles = sorted(set(payload.roles))
    server.visibility_user_ids = ids
    server.visibility_revision = (server.visibility_revision or 0) + 1
    db.commit()
    from app.agents.manager import agent_manager
    agent_manager.clear_cache()
    return visibility(server)
