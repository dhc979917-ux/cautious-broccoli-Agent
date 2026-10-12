"""MCP 服务：MCP Server 配置的 CRUD、连接测试、工具枚举，以及为 Agent 构建 MCPClient。

参考 nexent remote_mcp_app 的子集（本地 stdio + URL http 两类；不含社区市场/容器化）：
- stdio：command + args + env（在后端宿主机起子进程，与 PowerShell 工具同级能力）
- http：url（SSE / streamable-http）
只有 admin 可增删改启停（权限在 API 层校验），绑定给 Agent 后全员可用。
MCP 配置任何写操作后调 agent_manager.clear_cache() 让 Agent 重建生效。"""

import logging

logger = logging.getLogger(__name__)

import asyncio
import json
import ipaddress
import os
import shutil
import socket
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy.orm import Session as DBSession

from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig

from app.models.mcp_server import McpServer
from app.config import settings
from app.services.mcp_credentials import runtime_env, update_env

TEST_TIMEOUT_SECONDS = 15.0
_CHILD_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH"}
_BUNDLED_ENV = {
    "email_mcp_server.py": {"DATABASE_URL"},
}


def validate_destination(record: McpServer) -> None:
    if not settings.GOVERNANCE_TOOL_POLICY_ENABLED:
        return
    if record.type == "stdio":
        executable = shutil.which(record.command or "")
        allowed = {os.path.realpath(name.strip()) for name in
                   settings.MCP_STDIO_ALLOWED_COMMANDS.split(",") if name.strip()}
        if not executable or os.path.realpath(executable) not in allowed:
            raise ValueError("MCP stdio 命令不在管理员审核的可执行路径白名单中")
        return
    if record.type != "http":
        raise ValueError("MCP 类型不支持")
    parsed = urlsplit(record.url or "")
    reviewed_urls = {value.strip() for value in settings.MCP_HTTP_REVIEWED_URLS.split(",") if value.strip()}
    if record.url in reviewed_urls:
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or (
            parsed.password or parsed.fragment or parsed.query or parsed.hostname in ("localhost", "metadata.google.internal")
        ):
            raise ValueError("审核目标 URL 不安全")
        try:
            address = ipaddress.ip_address(parsed.hostname)
            if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
                raise ValueError("审核例外仅允许精确的内网服务 IP")
            if parsed.port is None:
                raise ValueError("审核例外必须明确端口")
        except ValueError as error:
            raise ValueError("审核例外必须使用内网 IP 与精确端口") from error
        return
    allowed_hosts = {name.strip().lower() for name in
                     settings.MCP_HTTP_ALLOWED_HOSTS.split(",") if name.strip()}
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise ValueError("MCP HTTP 端口无效") from error
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or (
        parsed.fragment or parsed.query or parsed.hostname.lower() not in allowed_hosts
    ):
        raise ValueError("MCP 出站请求仅允许管理员审核的 HTTPS 主机，无凭据、查询参数及片段")
    try:
        resolved = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise ValueError("MCP 目的主机 DNS 解析失败") from error
    if not resolved or any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise ValueError("MCP 目的地址不能为内网、回环、链路本地或未公开网段")


def _allowed_child_env(record: McpServer) -> dict:
    allowed = _CHILD_ENV | {
        name.strip() for name in settings.MCP_STDIO_ENV_PASSTHROUGH.split(",") if name.strip()
    }
    for argument in _parse_args(record.args_json):
        filename = os.path.basename(argument)
        bundled = _BUNDLED_ENV.get(filename)
        if bundled and os.path.realpath(argument) == os.path.realpath(
            os.path.join(os.path.dirname(__file__), "..", "agents", "mcp_servers", filename)
        ):
            allowed |= bundled
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _parse_args(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def build_config(record: McpServer, extra_env: Optional[dict] = None):
    """把 DB 记录转成 AgentScope MCP 配置。
    extra_env：按服务注入的额外环境变量（如邮件 MCP 的用户凭据），与记录自带 env 合并。"""
    validate_destination(record)
    if record.type == "stdio":
        # stdio 子进程默认只继承 mcp SDK 的白名单环境变量，拿不到 DATABASE_URL /
        # DASHSCOPE_API_KEY 等后端配置（子进程内会 import app.config，
        # 缺 DATABASE_URL 时会回落默认 sqlite，导致服务器上查不到表）。这里把后端
        # 当前环境整体并入，记录自带 env 与 extra_env 优先级更高。
        env = _allowed_child_env(record)
        env.update(runtime_env(record.env_json))
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        return StdioMCPConfig(
            command=record.command or "",
            args=_parse_args(record.args_json) or None,
            env=env or None,
        )
    return HttpMCPConfig(url=record.url or "")


def build_client(record: McpServer, extra_env: Optional[dict] = None) -> MCPClient:
    # AgentScope 2.0.5 的 MCPClient 要求显式 is_stateful（stdio 必须为 True）。
    # http 也按有状态处理：connect() 建会话后复用，兼容 SSE / streamable-http。
    return MCPClient(
        name=record.name,
        is_stateful=True,
        mcp_config=build_config(record, extra_env),
    )


async def connect_client(client: MCPClient) -> None:
    """有状态（stdio）客户端需要显式连接；http 无状态可跳过"""
    if getattr(client, "is_stateful", False):
        await client.connect()


async def test_connection(record: McpServer) -> dict:
    """连接并枚举工具，返回结果（15s 超时）"""
    client = None
    try:
        redactions = tuple(value for value in runtime_env(record.env_json).values() if value)

        def sanitized(value: str) -> str:
            for secret in redactions:
                value = value.replace(secret, "[redacted]")
            return value[:200]

        client = build_client(record)
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            await connect_client(client)
            tools = await client.list_raw_tools()
        tools = [
            {"name": sanitized(t.name), "description": sanitized(t.description or "")}
            for t in tools
        ]
        return {"ok": True, "tool_count": len(tools), "tools": tools, "error": None}
    except asyncio.TimeoutError:
        return {"ok": False, "tool_count": 0, "tools": [], "error": f"连接超时（>{int(TEST_TIMEOUT_SECONDS)}s）"}
    except Exception:
        return {"ok": False, "tool_count": 0, "tools": [], "error": "MCP connection failed; check the server configuration"}
    finally:
        try:
            if client is not None:
                await client.close(ignore_errors=True)
        except Exception:
            pass


class McpService:
    def __init__(self, db: DBSession):
        self.db = db

    def list_servers(self, include_deleted=False) -> List[McpServer]:
        query = self.db.query(McpServer)
        if not include_deleted:
            query = query.filter(McpServer.is_deleted.is_(False))
        return query.order_by(McpServer.id.asc()).all()

    def get(self, mcp_id: int) -> Optional[McpServer]:
        return self.db.query(McpServer).filter(McpServer.id == mcp_id).first()

    def get_by_ids(self, ids: List[int]) -> List[McpServer]:
        if not ids:
            return []
        return (
            self.db.query(McpServer)
            .filter(McpServer.id.in_(ids), McpServer.enabled.is_(True), McpServer.is_deleted.is_(False))
            .all()
        )

    def create(self, user_id: int, data: dict) -> McpServer:
        env_json, changed = update_env(None, data.get("env"))
        rec = McpServer(
            name=data["name"].strip(),
            type=data.get("type", "stdio"),
            command=data.get("command"),
            args_json=json.dumps(data.get("args") or [], ensure_ascii=False),
            env_json=env_json,
            env_rotated_at=datetime.now(timezone.utc) if changed else None,
            url=data.get("url"),
            description=data.get("description"),
            enabled=bool(data.get("enabled", True)),
            created_by=user_id,
        )
        validate_destination(rec)
        self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)
        self._invalidate_agents()
        return rec

    def update(self, mcp_id: int, data: dict) -> Optional[McpServer]:
        rec = self.get(mcp_id)
        if not rec:
            return None
        if rec.is_deleted:
            from fastapi import HTTPException
            raise HTTPException(409, 'MCP 服务已软删除，请先恢复')
        for field in ("name", "type", "command", "url", "description"):
            if field in data and data[field] is not None:
                setattr(rec, field, data[field])
        if "args" in data:
            rec.args_json = json.dumps(data.get("args") or [], ensure_ascii=False)
        if "env" in data:
            rec.env_json, changed = update_env(rec.env_json, data["env"])
            if changed:
                rec.env_rotated_at = datetime.now(timezone.utc)
        if "enabled" in data:
            rec.enabled = bool(data["enabled"])
        validate_destination(rec)
        self.db.commit()
        self.db.refresh(rec)
        self._invalidate_agents()
        return rec

    def toggle(self, mcp_id: int) -> Optional[McpServer]:
        rec = self.get(mcp_id)
        if not rec:
            return None
        if rec.is_deleted:
            from fastapi import HTTPException
            raise HTTPException(409, 'MCP 服务已软删除，请先恢复')
        rec.enabled = not rec.enabled
        self.db.commit()
        self.db.refresh(rec)
        self._invalidate_agents()
        return rec

    def delete(self, mcp_id: int) -> bool:
        rec = self.get(mcp_id)
        if not rec:
            return False
        rec.enabled = False
        rec.is_deleted = True
        self.db.commit()
        self._invalidate_agents()
        return True

    def restore(self, mcp_id: int) -> Optional[McpServer]:
        rec = self.get(mcp_id)
        if rec is None:
            return None
        if not rec.is_deleted:
            return rec
        rec.is_deleted = False
        rec.enabled = False
        self.db.commit()
        self._invalidate_agents()
        return rec

    def hard_delete(self, mcp_id: int) -> bool:
        from fastapi import HTTPException
        from app.models.agent import Agent
        from app.models.agent_governance import AgentVersion
        from app.models.tool_governance import AgentToolGrant, McpToolCatalog, ToolApproval
        rec = self.get(mcp_id)
        if rec is None:
            return False
        if not rec.is_deleted:
            raise HTTPException(409, '请先软删除 MCP 服务，再执行彻底删除')
        for agent in self.db.query(Agent).all():
            if mcp_id in (agent.config or {}).get('mcp_servers', []):
                raise HTTPException(409, f'MCP 仍被助手「{agent.name}」绑定，请先解除绑定并应用配置')
        for version in self.db.query(AgentVersion).filter_by(state='draft').all():
            snapshot = version.snapshot or {}
            if mcp_id in (snapshot.get('config') or {}).get('mcp_servers', []) or any(
                grant.get('server_id') == mcp_id for grant in snapshot.get('tool_grants') or []
            ):
                raise HTTPException(409, 'MCP 仍被待应用配置引用，请先解除绑定')
        for model in (ToolApproval, AgentToolGrant, McpToolCatalog):
            self.db.query(model).filter(model.server_id == mcp_id).delete(synchronize_session=False)
        self.db.delete(rec)
        self.db.commit()
        self._invalidate_agents()
        return True

    def _invalidate_agents(self):
        """配置变更后让 Agent 缓存失效（重建时才装载新的 MCP 工具）"""
        try:
            from app.agents.manager import agent_manager
            agent_manager.clear_cache()
        except Exception:
            pass

    async def build_clients(self, mcp_ids: List[int], per_server_env: Optional[dict] = None) -> List[MCPClient]:
        """为 Agent 并行构建并连接（stdio）启用的 MCP 客户端列表。
        per_server_env: {mcp_id: {env}}，按服务注入额外环境变量（如邮件 MCP 的用户凭据）。
        连接并行化：原串行实现 N 个 MCP 耗时叠加（3 个约 4s），并行后取最慢一个；
        单个连接失败仍不阻塞整体。"""
        per_server_env = per_server_env or {}
        records = self.get_by_ids(mcp_ids)

        async def _connect_one(rec: McpServer):
            t0 = time.monotonic()
            client = None
            connected = False
            try:
                client = build_client(rec, per_server_env.get(rec.id))
                timeout = max(1.0, settings.MCP_BUILD_TIMEOUT_SECONDS)
                await asyncio.wait_for(connect_client(client), timeout=timeout)
                # Warmup: ensure stdio subprocess has registered its tools
                # before CachingMCPClient wraps it (prevents empty tool list on first reset_tools)
                await asyncio.wait_for(client.list_raw_tools(), timeout=timeout)
                logger.info(f"[MCP] '{rec.name}' 连接完成，耗时 {time.monotonic() - t0:.1f}s")
                connected = True
                return client
            except Exception:
                # 单个 MCP 连接失败不阻塞 Agent 构建（其余工具照常可用）
                logger.error("[MCP] connection failed for server id=%s", rec.id)
                return None
            finally:
                if client is not None and not connected:
                    try:
                        await asyncio.wait_for(client.close(ignore_errors=True), timeout=5.0)
                    except (Exception, asyncio.CancelledError):
                        logger.warning("[MCP] 连接失败后关闭客户端超时: %s", rec.name)

        results = await asyncio.gather(*[_connect_one(r) for r in records])
        return [c for c in results if c is not None]
