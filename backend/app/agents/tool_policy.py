"""MCP Tool visibility and fail-closed checks at the AgentScope execution boundary."""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.config import settings
from app.models.agent import Agent
from app.models.agent_governance import AgentVersion, SessionAgentVersion
from app.models.mcp_server import McpServer
from app.models.session import Session as ChatSession
from app.models.tool_governance import AgentToolGrant, McpToolCatalog, ToolApproval
from app.models.user import User
from app.services.agent_access import can_execute_agent
from app.services.audit_service import begin_event, complete_event
from app.services.tool_governance import digest_schema, schema_of


logger = logging.getLogger(__name__)


def register_permission_rule(context, name, behavior):
    from agentscope.permission import PermissionEngine, PermissionRule
    PermissionEngine(context).add_rule(PermissionRule(
        tool_name=name, rule_content=None, behavior=behavior, source='server'))


def risk_behavior(risk, name, confirm_tools, enforce):
    from agentscope.permission import PermissionBehavior
    if enforce and risk not in ("low", "high"):
        return PermissionBehavior.DENY
    if risk == "high" or (risk != "low" and name in confirm_tools):
        return PermissionBehavior.ASK
    return PermissionBehavior.ALLOW


def protect_access(toolkit, agent_id, user_id, server_ids):
    if not server_ids:
        return
    from app.services.mcp_access import can_use
    prefixes = [(f"mcp__{server.name}__", server.id) for server in _servers(server_ids)]
    original = toolkit.call_tool

    async def guarded(tool_call, state):
        if tool_call.name.startswith("mcp__"):
            matches = [server_id for prefix, server_id in prefixes if tool_call.name.startswith(prefix)]
            with SessionLocal() as db:
                user = db.get(User, user_id) if user_id is not None else None
                if len(matches) != 1 or not can_use(db.get(McpServer, matches[0]) if matches else None, user):
                    denial = ToolChunk(content=[TextBlock(text="此 MCP 的使用授权已失效，请联系管理员。")],
                                       state=ToolResultState.DENIED)
                    yield denial
                    yield ToolResponse(id=tool_call.id).append_chunk(denial)
                    return
        async for chunk in original(tool_call, state):
            yield chunk

    toolkit.call_tool = guarded


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def argument_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def argument_summary(raw: str) -> str:
    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError):
        return "参数已隐藏"
    if not isinstance(arguments, dict):
        return "参数已隐藏"
    names = [key[:40] for key in arguments if isinstance(key, str)]
    return "字段：" + ("、".join(names[:8]) or "无") + "（值不展示）"


def registration_name(server_name: str, name: str) -> str:
    return f"mcp__{server_name}__{re.sub(r'[^a-zA-Z0-9_-]', 'x', name)}"


class VisibleMCPClient:
    def __init__(self, client, allowed: dict[str, str]):
        self._client = client
        self._allowed = allowed
        self._visible_names = None
        self.name = client.name

    async def list_raw_tools(self):
        tools = await self._client.list_raw_tools()
        if self._visible_names is None:
            self._visible_names = {tool.name for tool in tools if tool.name in self._allowed
                and (getattr(self._client, "original_digest", lambda _: None)(tool.name) or
                     digest_schema(tool.name, *schema_of(tool))) == self._allowed[tool.name]}
        return [tool for tool in tools if tool.name in self._visible_names]

    async def list_tools(self):
        return [await self._client.get_tool(tool.name) for tool in await self.list_raw_tools()]

    async def get_tool(self, name):
        if self._visible_names is None:
            await self.list_raw_tools()
        if name not in self._visible_names:
            raise PermissionError("Tool 未获授权")
        return await self._client.get_tool(name)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _pinned(db: Session, agent_id: int, session_id: int | None):
    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        return None
    if session_id is None:
        return {}
    pin = db.get(SessionAgentVersion, session_id)
    version = db.get(AgentVersion, pin.version_id) if pin else None
    if version is None or version.agent_id != agent_id or version.state != "published":
        return {}
    return version.snapshot


def visible_clients(db: Session, clients: list, server_ids: list[int], agent_id: int,
                    session_id: int | None = None) -> list:
    snapshot = _pinned(db, agent_id, session_id)
    servers = {row.name: row for row in db.query(McpServer).filter(
        McpServer.id.in_(server_ids), McpServer.enabled.is_(True), McpServer.is_deleted.is_(False))}
    grants = (db.query(AgentToolGrant).filter_by(agent_id=agent_id).all()
              if snapshot is None else snapshot.get("tool_grants", []))
    catalogs = {(row.server_id, row.name): row for row in db.query(McpToolCatalog).filter(
        McpToolCatalog.server_id.in_(server_ids))}
    allowed: dict[int, dict[str, str]] = {}
    for grant in grants:
        server_id = grant.server_id if snapshot is None else grant.get("server_id")
        name = grant.tool_name if snapshot is None else grant.get("tool_name")
        digest = grant.schema_digest if snapshot is None else grant.get("schema_digest")
        catalog = catalogs.get((server_id, name))
        if catalog is not None and catalog.available and catalog.risk in ("low", "high") and (
            catalog.schema_digest == digest
        ):
            allowed.setdefault(server_id, {})[name] = digest
    registrations: set[str] = set()
    ambiguous: set[str] = set()
    for server in servers.values():
        for name in allowed.get(server.id, {}):
            registered = registration_name(server.name, name)
            if registered in registrations:
                ambiguous.add(registered)
            registrations.add(registered)
    result = []
    for client in clients:
        server = servers.get(client.name)
        if server is not None:
            safe = {name: digest for name, digest in allowed.get(server.id, {}).items()
                    if registration_name(server.name, name) not in ambiguous}
            if safe:
                result.append(VisibleMCPClient(client, safe))
    return result


def risk_for(db: Session, agent_id: int, server_id: int, name: str,
             session_id: int | None = None) -> str | None:
    snapshot = _pinned(db, agent_id, session_id)
    grant = (db.query(AgentToolGrant).filter_by(agent_id=agent_id, server_id=server_id,
                                                tool_name=name).one_or_none()
             if snapshot is None else next((item for item in snapshot.get("tool_grants", [])
                 if item.get("server_id") == server_id and item.get("tool_name") == name), None))
    catalog = db.query(McpToolCatalog).filter_by(server_id=server_id, name=name).one_or_none()
    digest = grant.schema_digest if snapshot is None and grant else (
        grant.get("schema_digest") if grant else None)
    if grant and catalog and catalog.available and digest == catalog.schema_digest:
        return catalog.risk
    return None


def begin_confirmation(user_id: int, session_id: int, agent_id: int,
                       server_id: int, name: str, call_id: str, arguments: str) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        catalog = db.query(McpToolCatalog).filter_by(server_id=server_id, name=name).one_or_none()
        if not catalog or risk_for(db, agent_id, server_id, name, session_id) not in ("high", "low"):
            raise PermissionError("Tool 未获授权")
        audit_id = begin_event(action="approve_tool", resource_type="mcp",
                               resource_id=str(server_id), request_id=uuid4().hex,
                               trace_id=None, ip=None)
        entry = ToolApproval(
            user_id=user_id, session_id=session_id, agent_id=agent_id, server_id=server_id,
            tool_name=name, tool_call_id=call_id, arguments_digest=argument_digest(arguments),
            schema_digest=catalog.schema_digest, status="pending", expires_at=now + timedelta(seconds=60),
            audit_event_id=audit_id,
        )
        db.add(entry)
        db.commit()


def resolve_confirmation(session_id: int, call_id: str, approved: bool) -> None:
    with SessionLocal() as db:
        row = db.query(ToolApproval).filter_by(session_id=session_id,
                                                tool_call_id=call_id).with_for_update().one_or_none()
        if not row or row.status != "pending":
            return
        now = datetime.now(timezone.utc)
        row.status = "approved" if approved and _utc(row.expires_at) > now else "denied"
        row.resolved_at = now
        complete_event(row.audit_event_id, actor_id=row.user_id,
                       http_status=200 if row.status == "approved" else 403)
        db.commit()


def check_and_consume(db: Session, agent_id: int, user_id: int, session_id: int,
                      server_id: int, name: str, call_id: str, arguments: str, live_digest: str) -> None:
    user = db.get(User, user_id)
    session = db.get(ChatSession, session_id)
    server = db.get(McpServer, server_id)
    agent = db.get(Agent, agent_id)
    snapshot = _pinned(db, agent_id, session_id)
    bound = (snapshot.get("config") if snapshot is not None else agent.config or {}) if agent else {}
    if not user or not user.is_active or not session or session.user_id != user_id or (
        session.agent_id != agent_id or session.status != "active" or not agent or not agent.is_active
    ) or not server or not server.enabled or server.is_deleted or server_id not in {
        int(value) for value in (bound or {}).get("mcp_servers", [])
    }:
        raise PermissionError("会话、Agent 或 Server 授权已失效")
    if not can_execute_agent(db, user_id, agent_id, user.role):
        raise PermissionError("用户没有 Agent 执行权限")
    from app.services.mcp_access import can_use
    if not can_use(server, user):
        raise PermissionError("用户没有此 MCP 的使用权限")
    catalog = db.query(McpToolCatalog).filter_by(server_id=server_id, name=name).one_or_none()
    if not catalog or not catalog.available or catalog.schema_digest != live_digest:
        raise PermissionError("Tool schema 已变更或目录已下线")
    risk = risk_for(db, agent_id, server_id, name, session_id)
    if risk not in ("low", "high"):
        raise PermissionError("Tool 未被分类及授权")
    if risk == "high":
        approved = db.query(ToolApproval).filter_by(session_id=session_id,
            tool_call_id=call_id, user_id=user_id, agent_id=agent_id, server_id=server_id,
            tool_name=name).with_for_update().one_or_none()
        now = datetime.now(timezone.utc)
        if not approved or approved.status != "approved" or _utc(approved.expires_at) <= now or (
            approved.schema_digest != live_digest or approved.arguments_digest != argument_digest(arguments)
        ):
            raise PermissionError("危险操作未获本次调用的有效确认")
        approved.status = "consumed"
        approved.consumed_at = now
        db.commit()


def protect_toolkit(toolkit, clients: list, agent_id: int, user_id: int | None,
                    session_id: int | None, server_ids: list[int]) -> None:
    server_names = {row.name: row.id for row in _servers(server_ids)}
    by_registration = {}
    ambiguous = set()
    for client in clients:
        server_id = server_names.get(client.name)
        if server_id is None:
            continue
        for tool_name in client._allowed:
            registered = registration_name(client.name, tool_name)
            if registered in by_registration:
                ambiguous.add(registered)
            else:
                by_registration[registered] = (server_id, tool_name, client)
    for registered in ambiguous:
        by_registration.pop(registered, None)
    original = toolkit.call_tool

    async def guarded(tool_call, state):
        if not tool_call.name.startswith("mcp__"):
            async for chunk in original(tool_call, state):
                yield chunk
            return
        audit_id = None
        status = 403
        try:
            from opentelemetry import trace
            context = trace.get_current_span().get_span_context()
            current_trace_id = f'{context.trace_id:032x}' if context.is_valid else getattr(state, 'trace_id', None)
            audit_id = begin_event(action="invoke_tool", resource_type="mcp",
                                   resource_id=str(agent_id), request_id=uuid4().hex,
                                   trace_id=current_trace_id, ip=None)
            binding = by_registration.get(tool_call.name)
            if binding is None or user_id is None or session_id is None:
                raise PermissionError("Tool 未获授权")
            server_id, name, client = binding
            source = getattr(client._client, "_client", client._client)
            live = next((tool for tool in await source.list_raw_tools()
                         if tool.name == name), None)
            if live is None:
                raise PermissionError("Tool 不再可用")
            digest = digest_schema(name, *schema_of(live))
            with SessionLocal() as db:
                check_and_consume(db, agent_id, user_id, session_id, server_id, name,
                                  str(tool_call.id), tool_call.input, digest)
            status = 200
            async for chunk in original(tool_call, state):
                yield chunk
        except Exception as exc:
            logger.warning("MCP Tool invocation blocked (%s)", type(exc).__name__)
            status = 403 if isinstance(exc, PermissionError) else 503
            denial = ToolChunk(content=[TextBlock(text="MCP Tool 执行被策略阻止，请联系管理员复核授权。")],
                               state=ToolResultState.DENIED)
            yield denial
            yield ToolResponse(id=tool_call.id).append_chunk(denial)
        finally:
            if audit_id is not None:
                try:
                    complete_event(audit_id, actor_id=user_id, http_status=status)
                except Exception:
                    logger.error("MCP Tool 审计结果写入失败，已保留调用尝试记录")

    toolkit.call_tool = guarded


def _servers(server_ids: list[int]) -> list[McpServer]:
    with SessionLocal() as db:
        return [row for row in db.query(McpServer).filter(McpServer.id.in_(server_ids))]
