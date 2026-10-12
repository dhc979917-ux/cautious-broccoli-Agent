"""MCP 工具 schema 精简（P0-2：固定前缀瘦身）。

为什么需要：部分第三方 MCP server 的工具描述普遍冗长（实测某工单类
server 37 个工具、53,281 字符 ≈17.8k token）。
这些 schema 随每次模型调用全量重发，是 input token 的最大单一来源。

精简原则（只压散文，不动结构）：
- 工具 description：按句界截断到 MCP_SCHEMA_SLIM_TOOL_DESC_CHARS；
- 参数 description：递归遍历 inputSchema，截断到 MCP_SCHEMA_SLIM_PARAM_DESC_CHARS；
- 删除 examples 等纯示例键（模型靠参数名/类型/必填项即可正确调用）；
- 参数名、类型、required、enum、$defs/anyOf 等结构一律保留，不影响调用正确性。

作用点：CachingMCPClient 首次 list_raw_tools 后原地精简——raw Tool 对象与
MCPClient._cached_tools 共享引用，get_tool()/list_tools() 包装 MCPTool 时读到的
就是精简后的 description/inputSchema，一次处理、全链路（schema 下发 + 权限注册）
生效。工具名不变，权限规则（mcp__<server>__<tool>）不受影响。

豁免：MCP_SCHEMA_SLIM_EXCLUDE_SERVERS 列出的 server 名原样保留。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 句界候选（中文句号/感叹/问号、换行、英文句点）；截断优先落在这些位置
_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?\n.]|;\s")

# schema 遍历时要下钻的组合键
_SCHEMA_NEST_KEYS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
# 纯示例/展示型注解键：删掉不影响模型理解参数语义
_SCHEMA_DROP_KEYS = ("examples", "example", "x-examples", "markdownDescription")


def _truncate_at_sentence(text: str, limit: int) -> str:
    """按句界截断超长文本；找不到句界则硬截。附加省略标记。"""
    if len(text) <= limit:
        return text
    cut = -1
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        if m.start() >= limit:
            break
        cut = m.start()
    if cut < max(20, limit // 3):
        # 前段没有可用句界（一段长散文），硬截在 limit 处
        cut = limit
    return text[:cut].rstrip() + "…"


def _slim_schema_node(node: Any, param_desc_chars: int) -> None:
    """递归精简 JSON schema 节点：截断 description、删示例键、下钻嵌套结构。"""
    if isinstance(node, dict):
        desc = node.get("description")
        if isinstance(desc, str) and len(desc) > param_desc_chars:
            node["description"] = _truncate_at_sentence(desc, param_desc_chars)
        for key in _SCHEMA_DROP_KEYS:
            node.pop(key, None)
        for key in _SCHEMA_NEST_KEYS:
            sub = node.get(key)
            if isinstance(sub, dict):
                for child in sub.values():
                    _slim_schema_node(child, param_desc_chars)
        for key in _SCHEMA_LIST_KEYS:
            sub = node.get(key)
            if isinstance(sub, list):
                for child in sub:
                    _slim_schema_node(child, param_desc_chars)
        items = node.get("items")
        if items is not None:
            _slim_schema_node(items, param_desc_chars)
    elif isinstance(node, list):
        for child in node:
            _slim_schema_node(child, param_desc_chars)


def slim_raw_tools(
    tools: list,
    tool_desc_chars: int,
    param_desc_chars: int,
) -> dict:
    """原地精简 raw MCP Tool 列表（mcp.types.Tool，pydantic 模型可原地赋值）。

    返回统计 {"tools": 总数, "slimmed": 被精简数, "chars_before/after": 描述总字符}，
    供调用方打日志。异常（个别 Tool 结构异常）不向上抛，跳过该工具即可。
    """
    stats = {"tools": 0, "slimmed": 0, "chars_before": 0, "chars_after": 0}
    for tool in tools or []:
        try:
            stats["tools"] += 1
            before = len(getattr(tool, "description", "") or "")
            schema = getattr(tool, "inputSchema", None)
            schema_before = len(str(schema)) if schema else 0
            stats["chars_before"] += before + schema_before

            if before > tool_desc_chars:
                tool.description = _truncate_at_sentence(
                    tool.description, tool_desc_chars
                )
            if isinstance(schema, dict):
                _slim_schema_node(schema, param_desc_chars)

            after = len(getattr(tool, "description", "") or "")
            schema_after = len(str(schema)) if schema else 0
            stats["chars_after"] += after + schema_after
            if (after + schema_after) < (before + schema_before):
                stats["slimmed"] += 1
        except Exception as e:
            logger.debug("[mcp_slim] 精简工具 %s 失败（跳过）: %s",
                         getattr(tool, "name", "?"), e)
    return stats


def slim_config_for(server_name: str) -> dict | None:
    """按 server 名取精简配置；返回 None 表示该 server 豁免或全局关闭。

    配置读 app.config.settings（每次调用现读，改 .env 重启即生效，无缓存口径漂移）。
    """
    from app.config import settings

    if not getattr(settings, "MCP_SCHEMA_SLIM_ENABLED", False):
        return None
    excluded = {
        s.strip()
        for s in str(getattr(settings, "MCP_SCHEMA_SLIM_EXCLUDE_SERVERS", "") or "").split(",")
        if s.strip()
    }
    if server_name in excluded:
        return None
    return {
        "tool_desc_chars": int(getattr(settings, "MCP_SCHEMA_SLIM_TOOL_DESC_CHARS", 300) or 300),
        "param_desc_chars": int(getattr(settings, "MCP_SCHEMA_SLIM_PARAM_DESC_CHARS", 120) or 120),
    }
