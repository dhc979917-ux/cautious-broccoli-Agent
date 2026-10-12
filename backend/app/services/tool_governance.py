"""Reviewed Tool discovery and grants, separate from legacy MCP connection tests."""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.mcp_server import McpServer
from app.models.tool_governance import AgentToolGrant, McpToolCatalog
from app.services.mcp_service import build_client, connect_client


HIGH_RISK_NAME = re.compile(
    r"(?i)(?:delete|remove|drop|truncate|write|update|insert|create|send|reply|"
    r"execute|exec|run|shell|grant|permission|revoke|batch|bulk|upload|commit)"
)
UNSAFE_SCHEMA_KEY = {"default", "examples", "example"}


def digest_schema(name: str, input_schema: dict, output_schema: dict | None) -> str:
    payload = json.dumps([name, input_schema, output_schema], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def schema_of(tool) -> tuple[dict, dict | None]:
    input_schema = getattr(tool, "inputSchema", {}) or {}
    output_schema = getattr(tool, "outputSchema", None)
    if not isinstance(input_schema, dict) or (output_schema is not None and not isinstance(output_schema, dict)):
        raise ValueError("MCP Tool schema is invalid")
    if len(json.dumps([input_schema, output_schema], ensure_ascii=False)) > 120_000:
        raise ValueError("MCP Tool schema exceeds limit")
    return input_schema, output_schema


def public_schema(value):
    if isinstance(value, dict):
        return {key: public_schema(item) for key, item in value.items()
                if key not in UNSAFE_SCHEMA_KEY}
    if isinstance(value, list):
        return [public_schema(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:1000]
    return value


def summary(tool: McpToolCatalog) -> dict:
    from app.services.mcp_descriptions import clean_description
    return {
        "server_id": tool.server_id, "name": tool.name,
        "description": tool.display_description or clean_description(tool.description),
        "description_status": tool.description_status,
        "suggested_risk": "high" if HIGH_RISK_NAME.search(tool.name) else "low",
        "schema_digest": tool.schema_digest, "input_schema": tool.input_schema,
        "output_schema": tool.output_schema, "risk": tool.risk,
        "available": tool.available, "revision": tool.revision,
        "discovered_at": tool.discovered_at.isoformat() if tool.discovered_at else None,
    }


async def refresh_catalog(db: Session, server_id: int) -> dict:
    server = db.get(McpServer, server_id)
    if server is None:
        raise HTTPException(404, "MCP Server 不存在")
    if not server.enabled:
        raise HTTPException(409, "MCP Server 已停用")
    client = None
    try:
        client = build_client(server)
        async with asyncio.timeout(15):
            await connect_client(client)
            found = await client.list_raw_tools()
    except (asyncio.TimeoutError, Exception) as exc:
        raise HTTPException(503, "MCP 工具发现失败；历史目录未更改") from exc
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(5):
                    await client.close(ignore_errors=True)
            except (Exception, asyncio.CancelledError):
                pass

    existing = {row.name: row for row in db.query(McpToolCatalog).filter_by(server_id=server_id)}
    seen: set[str] = set()
    changes: list[dict] = []
    moment = datetime.now(timezone.utc)
    for tool in found:
        name = getattr(tool, "name", "")
        if not isinstance(name, str) or not name or len(name) > 200 or name in seen:
            raise HTTPException(502, "MCP 工具目录包含重复或非法标识；未更新目录")
        seen.add(name)
        try:
            input_schema, output_schema = schema_of(tool)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        digest = digest_schema(name, input_schema, output_schema)
        row = existing.get(name)
        kind = "unchanged"
        if row is None:
            row = McpToolCatalog(server_id=server_id, name=name, schema_digest=digest,
                                 input_schema={}, risk="unknown",
                                 revision=0)
            db.add(row)
            kind = "new"
        elif row.schema_digest != digest:
            row.risk = "unknown"
            kind = "schema_changed"
        elif not row.available:
            kind = "returned"
        row.schema_digest = digest
        from app.services.mcp_descriptions import digest_description
        original_description = str(getattr(tool, "description", "") or "")[:12000]
        description_digest = digest_description(name, original_description,
                                                public_schema(input_schema), public_schema(output_schema))
        if row.description_digest != description_digest:
            row.description_status = "pending"
        row.description = original_description
        row.input_schema = public_schema(input_schema)
        row.output_schema = public_schema(output_schema)
        row.available = True
        row.discovered_at = moment
        if kind != "unchanged":
            row.revision += 1
            changes.append({"name": name, "change": kind})
    for row in existing.values():
        if row.name not in seen and row.available:
            row.available = False
            row.revision += 1
            changes.append({"name": row.name, "change": "missing"})
    db.commit()
    from app.services.mcp_descriptions import enqueue_descriptions
    enqueue_descriptions()
    return {"server_id": server_id, "changes": changes, "tool_count": len(found),
            "discovered_at": moment.isoformat()}


def list_catalog(db: Session, server_id: int) -> list[dict]:
    if db.get(McpServer, server_id) is None:
        raise HTTPException(404, "MCP Server 不存在")
    return [summary(row) for row in db.query(McpToolCatalog).filter_by(
        server_id=server_id).order_by(McpToolCatalog.name)]


def set_risk(db: Session, server_id: int, name: str, revision: int, risk: str) -> dict:
    row = db.query(McpToolCatalog).filter_by(server_id=server_id, name=name).with_for_update().one_or_none()
    if row is None:
        raise HTTPException(404, "Tool 不存在")
    if row.revision != revision:
        raise HTTPException(409, "Tool 已变更，请重新检查 schema")
    if not row.available or not db.get(McpServer, server_id).enabled:
        raise HTTPException(409, "Tool 或 Server 已下线")
    row.risk = risk
    row.revision += 1
    db.commit()
    from app.agents.manager import agent_manager
    agent_manager.clear_cache()
    return summary(row)


def replace_grants(db: Session, agent_id: int, actor_id: int, choices: list[dict]) -> list[dict]:
    from app.config import settings
    if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        raise HTTPException(409, "Agent 已启用版本治理，请通过 Agent 草稿管理 Tool 白名单")
    agent = db.get(Agent, agent_id, with_for_update=True)
    if agent is None:
        raise HTTPException(404, "Agent 不存在")
    bindings = {int(value) for value in (agent.config or {}).get("mcp_servers", [])}
    if len(choices) != len({(item["server_id"], item["tool_name"]) for item in choices}):
        raise HTTPException(422, "Tool 授权有重复项")
    verified = []
    for choice in choices:
        server_id, name, digest = choice["server_id"], choice["tool_name"], choice["schema_digest"]
        server = db.get(McpServer, server_id)
        row = db.query(McpToolCatalog).filter_by(server_id=server_id, name=name).one_or_none()
        if server_id not in bindings or server is None or not server.enabled:
            raise HTTPException(409, "Tool 不在 Agent 当前启用的 Server 绑定中")
        if row is None or not row.available or row.risk == "unknown" or row.schema_digest != digest:
            raise HTTPException(409, "Tool 尚未分类或 schema 已变化")
        verified.append(AgentToolGrant(agent_id=agent_id, server_id=server_id,
                                       tool_name=name, schema_digest=digest, granted_by=actor_id))
    db.query(AgentToolGrant).filter_by(agent_id=agent_id).delete(synchronize_session=False)
    db.add_all(verified)
    db.commit()
    from app.agents.manager import agent_manager
    agent_manager.clear_cache()
    return [{"server_id": grant.server_id, "tool_name": grant.tool_name,
             "schema_digest": grant.schema_digest} for grant in verified]
