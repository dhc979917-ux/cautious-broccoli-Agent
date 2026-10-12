"""MCP 工具列表会话级缓存。

AgentScope 的 ReAct 循环每轮都会调用 toolkit.get_tool_schemas()，进而对每个
MCP 客户端 list_tools()（见 agentscope/tool/_toolkit.py:get_tool_schemas）。
对 stdio 型 MCP，每次 list_tools 都是一次子进程往返（ListToolsRequest），N 轮
迭代就 N 次往返，既产生日志噪声也增加每轮延迟。工具清单在一次会话内是静态的，
缓存首结果即可消除重复拉取。

本代理为鸭子类型：缓存 list_tools / list_raw_tools，其余方法/属性
（connect / close / call_tool / name / is_stateful ...）透传到底层 MCPClient。
AgentScope 的 Toolkit 仅用 client.name 与 await client.list_tools()，且无
isinstance(client, MCPClient) 校验，故代理安全。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CachingMCPClient:
    """透传式代理：缓存 list_tools/list_raw_tools 首结果，其余委托底层客户端。

    slim=True 时在首次拉取后原地精简工具 schema（截断冗长 description、删示例键，
    见 app/agents/mcp_schema_slim.py）。raw Tool 对象与底层 client._cached_tools
    共享引用，因此 list_tools/get_tool 走哪条路拿到的都是精简结果。
    """

    def __init__(self, client: Any, slim: bool = False) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_tools_cache", None)
        object.__setattr__(self, "_raw_cache", None)
        object.__setattr__(self, "_slim", slim)
        object.__setattr__(self, "_slimmed", False)
        object.__setattr__(self, "_original_digests", {})

    @property
    def name(self) -> str:
        return self._client.name

    def _maybe_slim(self, tools) -> None:
        """首次拉取后原地精简（幂等：已精简的描述不会二次截断）。"""
        if not object.__getattribute__(self, "_slim") or object.__getattribute__(self, "_slimmed"):
            return
        object.__setattr__(self, "_slimmed", True)
        try:
            from app.services.tool_governance import digest_schema, schema_of
            object.__setattr__(self, "_original_digests", {
                tool.name: digest_schema(tool.name, *schema_of(tool)) for tool in tools
            })
            from app.agents.mcp_schema_slim import slim_config_for, slim_raw_tools
            cfg = slim_config_for(self.name)
            if not cfg or not tools:
                return
            stats = slim_raw_tools(tools, cfg["tool_desc_chars"], cfg["param_desc_chars"])
            if stats.get("slimmed"):
                logger.info(
                    "[mcp_slim] server=%s 精简 %d/%d 个工具，schema 字符 %d→%d",
                    self.name, stats["slimmed"], stats["tools"],
                    stats["chars_before"], stats["chars_after"],
                )
        except Exception as e:  # 精简失败绝不影响工具可用性
            logger.debug("[mcp_slim] server=%s 精简失败（忽略）: %s", self.name, e)

    def original_digest(self, name: str) -> str | None:
        return object.__getattribute__(self, "_original_digests").get(name)

    async def list_tools(self):
        cache = object.__getattribute__(self, "_tools_cache")
        if cache is None:
            client = object.__getattribute__(self, "_client")
            # 不直接调 client.list_tools()：它内部会重新 list_raw_tools() 一次
            # （多一次 ListToolsRequest 往返），且 MCPTool 构造发生在那次全新抓取
            # 之后，精简标记会丢。这里手动两阶段：先取 raw（缓存进 _cached_tools）
            # → 原地精简 → 再 get_tool 逐个包装，保证 MCPTool 拿到的是精简后的描述。
            raw = await client.list_raw_tools()
            self._maybe_slim(raw)
            cache = [await client.get_tool(t.name) for t in raw]
            object.__setattr__(self, "_tools_cache", cache)
        return cache

    async def list_raw_tools(self):
        cache = object.__getattribute__(self, "_raw_cache")
        if cache is None:
            client = object.__getattribute__(self, "_client")
            cache = await client.list_raw_tools()
            self._maybe_slim(cache)
            object.__setattr__(self, "_raw_cache", cache)
        return cache

    def __getattr__(self, item: str) -> Any:
        # 仅在常规属性查找失败时触发：connect/close/call_tool/is_stateful 等全部透传。
        # _client 未初始化时直接抛 AttributeError，避免 __getattr__ 递归。
        if item == "_client":
            raise AttributeError(item)
        return getattr(object.__getattribute__(self, "_client"), item)
