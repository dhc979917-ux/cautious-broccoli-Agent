"""并行工具同参去重（编码强制，非提示词）。

traces 实测：模型曾在同一并行调用批次里发出两次参数完全相同的工具调用
（如两次 CreatePlan、两次相同 Glob），每次都带着完整上下文执行，凭空
多耗数万 token。本中间件识别同一 Agent 实例内"同名工具 + 同参数"的
并发调用：第一个调用正常执行，并发重复调用等待并直接复用其结果，
不重复执行。仅在第一个调用执行期间去重（in-flight），执行完成后立即
摘除记录，因此先后发生的合法同参调用（重试/复核）不受影响。

实例在 architectures/base.py 的 build() 里按会话创建，多个工具共享，
作用域天然与 Agent 实例（会话）一致，无跨用户串扰。
"""

import asyncio
import hashlib
import json
from collections import OrderedDict
from typing import Any, AsyncGenerator, Callable

from agentscope.tool import ToolMiddlewareBase
from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk

_MAX_INFLIGHT = 256


def _arg_key(input_kwargs: dict[str, Any]) -> str:
    """参数字典 → 稳定哈希（忽略下划线开头的注入参数）。"""
    kwargs = {k: v for k, v in input_kwargs.items() if not k.startswith("_")}
    try:
        raw = json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = repr(sorted(kwargs.items(), key=lambda kv: kv[0]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ParallelDedupMiddleware(ToolMiddlewareBase):
    """并发去重：同名工具 + 同参数的并发调用只执行一次，重复者复用结果。"""

    def __init__(self) -> None:
        # key -> {"event": asyncio.Event, "chunks": [ToolChunk, ...]}
        self._inflight: "OrderedDict[str, dict]" = OrderedDict()

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        key = f"{tool.name}:{_arg_key(input_kwargs)}"

        inflight = self._inflight.get(key)
        if inflight is not None:
            # 并发重复调用：等首个调用结束，原样回放其结果
            await inflight["event"].wait()
            for chunk in list(inflight["chunks"]):
                yield chunk
            return

        entry: dict = {"event": asyncio.Event(), "chunks": []}
        self._inflight[key] = entry
        while len(self._inflight) > _MAX_INFLIGHT:
            self._inflight.popitem(last=False)
        try:
            async for chunk in next_handler(**input_kwargs):
                entry["chunks"].append(chunk)
                yield chunk
        finally:
            entry["event"].set()
            # 执行结束即摘除：不影响后续合法的同参调用
            self._inflight.pop(key, None)
