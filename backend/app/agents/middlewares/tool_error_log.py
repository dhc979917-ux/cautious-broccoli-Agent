"""工具失败可观测：把 state=error/denied 的工具调用打成一条结构化日志。

为什么需要（2026-09-10 会话 276 排查实录）：
- 框架 TracingMiddleware 只在工具**抛异常**时把 span 标成 ERROR；内置工具几乎
  从不抛异常，而是返回 ``ToolChunk(state=ERROR)``（"must first read"、
  "old_string not found"、命令非零退出……），于是 traces 里这些 span 全是正常态；
- 平台侧只有 manager 汇总的 ``tool_error_count`` 计数，知道"错了 3 次"却不知道
  错在哪个工具、哪一轮、报的什么，排查只能靠翻消息 metadata 里的截断输出。

做法：用官方 ``on_acting`` 钩子（只包住 ``toolkit.call_tool`` 这一层纯执行）原样
透传所有 chunk，仅在有 error/denied 时补一条 WARNING 日志，带上会话/回复/轮次/
工具名/入参摘要/错误首行。不改事件、不改上下文、不吞异常，与 tracing、预算、
压缩等其它中间件互不影响；框架将来若在 span 上标了 ERROR，本中间件也只是多一条
日志，不会冲突。
"""

import json
import logging
from typing import Any, AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.message import ToolResultState
from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# 入参摘要上限：够定位问题（file_path / command 前缀），不至于把大段脚本抄进日志
_MAX_INPUT_CHARS = 300
# 错误正文只取首行：内置工具的错误文案第一句就是原因，后面多是引导话术
_MAX_REASON_CHARS = 400

# 需要记日志的终态（RUNNING/SUCCESS 不算失败；INTERRUPTED 是用户停止，另有链路记录）
_FAILURE_STATES = {ToolResultState.ERROR, ToolResultState.DENIED}


def _tool_name(tool_call: Any) -> str:
    """从 ToolCallBlock 取工具名（框架里它是 TypedDict，也兼容对象形态）。"""
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return str(getattr(tool_call, "name", "") or "")


def _input_brief(tool_call: Any) -> str:
    """入参摘要：JSON 单行 + 截断，日志里能一眼看出操作对象。"""
    if isinstance(tool_call, dict):
        raw = tool_call.get("input")
    else:
        raw = getattr(tool_call, "input", None)
    if not raw:
        return ""
    try:
        text = json.dumps(raw, ensure_ascii=False, default=str)
    except Exception:
        text = str(raw)
    text = " ".join(text.split())
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS] + "..."
    return text


def _reason(item: Any) -> str:
    """从 ToolChunk/ToolResponse 里取错误首行。"""
    parts = []
    for block in getattr(item, "content", None) or []:
        text = getattr(block, "text", None)
        if not text and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    # 多个文本块按行拼接，再取第一条非空行：内置工具的错误文案首句就是原因，
    # 后面往往跟着几行引导话术，抄进日志只会淹没关键信息。
    first_line = ""
    for line in "\n".join(parts).splitlines():
        line = " ".join(line.split())
        if line:
            first_line = line
            break
    if not first_line:
        return ""
    if len(first_line) > _MAX_REASON_CHARS:
        first_line = first_line[:_MAX_REASON_CHARS] + "..."
    return first_line


class ToolErrorLogMiddleware(MiddlewareBase):
    """只加日志、不改行为的工具失败观测中间件（挂在 Agent 中间件栈上）。"""

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        name = _tool_name(tool_call)
        failure = None
        try:
            async for item in next_handler(**input_kwargs):
                state = getattr(item, "state", None)
                if state in _FAILURE_STATES and failure is None:
                    # 流式工具可能有多个 error chunk，只留第一个（真正的失败原因）
                    failure = (state, _reason(item))
                yield item
        finally:
            if failure is not None:
                self._log(agent, name, tool_call, failure[0], failure[1])

    @staticmethod
    def _log(agent, name: str, tool_call: Any, state, reason: str) -> None:
        state_obj = getattr(agent, "state", None)
        session_id = getattr(state_obj, "session_id", "") or ""
        reply_id = getattr(state_obj, "reply_id", "") or ""
        cur_iter = getattr(state_obj, "cur_iter", "")
        try:
            logger.warning(
                "[tool_error] session=%s reply=%s iter=%s tool=%s state=%s input=%s reason=%s",
                session_id,
                reply_id,
                cur_iter,
                name or "<unknown>",
                getattr(state, "value", state),
                _input_brief(tool_call) or "-",
                reason or "<无文本输出>",
            )
        except Exception as e:  # 观测绝不能影响工具执行
            logger.debug("[tool_error] 记录失败（忽略）: %s", e)
