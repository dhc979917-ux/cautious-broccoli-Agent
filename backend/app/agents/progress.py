# -*- coding: utf-8 -*-
"""构建阶段进度事件通道。

Agent 构建（记忆库供应 / MCP 连接 / 沙箱初始化）在服务器上可能耗时数秒至数十秒，
期间前端原先收不到任何事件，用户误以为卡死。WS worker 经 contextvar 注入
emit 回调，build 关键里程碑发 status 事件，前端据此渲染进行中指示。
"""
import contextvars
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

ProgressSink = Callable[[str], Awaitable[None]]

current_progress: contextvars.ContextVar[Optional[ProgressSink]] = contextvars.ContextVar(
    "current_progress", default=None
)


async def progress(message: str) -> None:
    """发一条构建阶段进度消息；无通道或发送失败时静默忽略。"""
    sink = current_progress.get()
    if sink is None:
        return
    try:
        await sink(message)
    except Exception as e:
        logger.debug(f"[progress] emit 失败: {e}")
