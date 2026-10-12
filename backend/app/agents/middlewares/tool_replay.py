"""ReadOnlyReplayMiddleware：同参重复只读调用去重（跨轮次，对照 Codex/Claude Code）。

为什么需要（2026-09-22 大纲实证）：
session 386 里模型连续两轮以相同参数调用 lookup_user_email("叶仕军")——第一次
已拿到明文地址，第二次是纯浪费：带着 31k 上下文白跑一轮模型调用。这类"模型
反复确认"行为在只读查询工具上普遍发生。

与 ParallelDedupMiddleware 的区别：那个管"同一并行批次内"的并发重复；本中间件
管"同一次回复内跨轮次"的先后重复。写操作/时间敏感工具不在此列——白名单制，
只有 TOOL_REPLAY_DEDUP_TOOLS 列出的纯只读工具参与；且只在**同一次回复**
（reply_id 不变）内重放，跨回复一律真实执行（避免陈旧数据）。

命中时直接重放首次结果（附一行"复用"提示），不再真实执行；未命中则正常执行
并把成功结果记入缓存。执行失败的结果不缓存（失败重试必须真实发生）。
"""

import json
import logging
from typing import Any, AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolChunk, ToolResponse

from app.config import settings
from app.agents.middlewares.tool_dedup import _arg_key

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# 每个 reply 最多缓存多少个工具结果（防长回复内无界增长；超出丢最旧）
_MAX_CACHE_PER_REPLY = 64


def _whitelist() -> set:
    raw = str(getattr(settings, "TOOL_REPLAY_DEDUP_TOOLS", "") or "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _tool_name(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return str(getattr(tool_call, "name", "") or "")


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _is_whitelisted(name: str, whitelist: set) -> bool:
    """精确名或 mcp__<server>__<tool> 短名命中即算（配置两种写法都支持）。"""
    if not name:
        return False
    if name in whitelist:
        return True
    return name.rsplit("__", 1)[-1] in whitelist


def _parse_input(tool_call: Any) -> dict | None:
    raw = tool_call.get("input") if isinstance(tool_call, dict) else getattr(tool_call, "input", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return None
    return {}


class ReadOnlyReplayMiddleware(MiddlewareBase):
    """同一回复内白名单只读工具的同参重复调用直接重放首次结果。"""

    def __init__(self) -> None:
        self._reply_id: str = ""
        self._cache: dict = {}   # "tool:arghash" -> 结果文本

    def _roll_reply(self, agent: "Agent") -> None:
        """reply_id 变化即清空缓存（跨回复不重放，保证数据新鲜）。"""
        reply_id = str(getattr(getattr(agent, "state", None), "reply_id", "") or "")
        if reply_id != self._reply_id:
            self._reply_id = reply_id
            self._cache = {}

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        name = _tool_name(tool_call)
        whitelist = _whitelist()
        if (
            not getattr(settings, "TOOL_REPLAY_DEDUP_ENABLED", False)
            or not whitelist
            or not _is_whitelisted(name, whitelist)
        ):
            async for item in next_handler(**input_kwargs):
                yield item
            return

        self._roll_reply(agent)
        args = _parse_input(tool_call)
        if args is None:
            # 参数都解析不了时不做判定，原样放行（绝不误伤正常调用）
            async for item in next_handler(**input_kwargs):
                yield item
            return
        key = f"{name}:{_arg_key(args)}"

        cached = self._cache.get(key)
        if cached is not None:
            logger.info(
                "[tool_replay] session=%s 命中重复调用 %s，重放首次结果（未执行）",
                getattr(agent.state, "session_id", "?"), name,
            )
            chunk = ToolChunk(
                content=[TextBlock(
                    text="（复用结果）本次回复中已用相同参数调用过该工具，结果如下，"
                         "请直接使用，不要重复调用：\n" + cached,
                )],
                state=ToolResultState.SUCCESS,
            )
            yield chunk
            yield ToolResponse(id=_tool_call_id(tool_call)).append_chunk(chunk)
            return

        # 正常执行，收集最终 ToolResponse 文本缓存起来
        final_text = None
        async for item in next_handler(**input_kwargs):
            state = getattr(item, "state", None)
            if state == ToolResultState.SUCCESS:
                texts = [
                    str(getattr(b, "text", "") or (b.get("text") if isinstance(b, dict) else "") or "")
                    for b in (getattr(item, "content", None) or [])
                ]
                merged = "\n".join(t for t in texts if t)
                if merged:
                    final_text = merged
            elif state in (ToolResultState.ERROR, ToolResultState.DENIED):
                final_text = None  # 失败结果不缓存，重试必须真实执行
            yield item

        if final_text:
            cap = int(getattr(settings, "TOOL_REPLAY_MAX_RESULT_CHARS", 4000) or 4000)
            if len(final_text) > cap:
                final_text = final_text[:cap] + "\n…[重放缓存已截断]…"
            while len(self._cache) >= _MAX_CACHE_PER_REPLY:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = final_text
