"""在容器里跑一条命令并取回 stdout —— 统一抹平 aiodocker 各版本的 exec 差异。

为什么单独抽一层：aiodocker 0.27 的 ``Exec.start()`` 是**同步**方法，直接返回
Stream 对象；早期版本它是 coroutine。直接写 ``await proc.start()`` 在新版本上会抛
"object Stream can't be used in 'await' expression"（实测踩到，导致服务探测整体失效、
预览发不出去）。这里对每一步都做 isawaitable 判断，两个版本都能跑，
调用方（service_probe 探测监听端口、preview_service 做连通性确认）不必关心版本。
"""

import asyncio
import inspect
import logging
from typing import List, Sequence

logger = logging.getLogger(__name__)

# 单条命令的读超时与输出上限：探测/确认都是短命令，给足余量但不放任无限输出
DEFAULT_TIMEOUT = 15.0
DEFAULT_LIMIT = 256 * 1024


async def _resolve(value):
    """是 awaitable 就 await，不是就原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def exec_capture(
    container,
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """在已存在的容器里执行 argv，返回 stdout 文本（UTF-8，替换非法字节）。

    只收 stdout、丢弃 stderr：调用方要的是结构化输出（JSON / 单行状态），
    混进 stderr 噪声还得再洗一遍。失败一律抛给调用方处理，本函数不吞异常。
    """
    proc = await _resolve(container.exec(list(argv), stdout=True, stderr=False))
    stream = await _resolve(proc.start())
    chunks: List[bytes] = []
    total = 0
    try:
        while True:
            message = await asyncio.wait_for(stream.read_out(), timeout=timeout)
            if message is None:
                break
            data = getattr(message, "data", b"") or b""
            if not data:
                continue
            chunks.append(data)
            total += len(data)
            if total >= limit:
                logger.debug("[docker-exec] 输出超过 %d 字节，提前截断", limit)
                break
    finally:
        closer = getattr(stream, "close", None)
        if closer is not None:
            try:
                await _resolve(closer())
            except Exception:
                pass
    return b"".join(chunks).decode("utf-8", "replace")
