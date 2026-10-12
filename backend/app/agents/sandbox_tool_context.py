"""沙箱感知的文件读缓存：让 Read/Write/Edit 的"先读后写"约束在 docker 沙箱下真正生效。

为什么需要（2026-09-10 会话 276 实测结论）：
- 框架 ``ToolContext.cache_file/get_cache``（``agentscope/state/_state.py``）用
  ``aiofiles.os.path.getmtime`` 在**后端进程本地**的文件系统上 stat；
- docker 沙箱模式下工具真正操作的是沙箱容器内的 ``/workspace/...``，这个路径在
  后端容器里不存在 → ``cache_file`` 的 getmtime 抛异常被静默吞掉（直接 return，
  不写缓存）→ ``get_cache`` 恒返回 None；
- 于是 ``Edit`` 与"覆盖已存在文件的 Write"100% 失败，报
  "To edit a file, you must first read it using the Read tool."
  （``tool/_builtin/_edit.py:304``、``_write.py:257``）。模型被迫盲改，
  进而引发一连串脚本级错误（会话 276 的 python-pptx RGBColor ValueError）。

做法：继承 ``ToolContext``，把两处 stat 换成走**工具自己的 backend**——
``BackendBase`` 已有 ``stat_mtime``（沙箱内 ``stat -c %Y``，本地 ``os.stat``），
路径语义与工具执行环境天然一致。只覆写 ``get_cache``/``cache_file`` 两个方法，
其余（LRU 上限、``clean_file_cache``、``activated_groups``）沿用框架实现。

扩展性：
- 指纹是 ``(mtime, size)`` 二元组而不是裸 mtime。沙箱侧 ``stat -c %Y`` 只有秒级
  精度，同一秒内"读→写→再写"会让陈旧缓存被误判为有效，第二次 Edit 基于旧内容
  替换会静默回滚第一次改动；带上 size 即可区分绝大多数同秒改写。
- 取指纹的实现以 ``stat_fn`` 形式注入（``local_stat`` / ``remote_stat``），
  换沙箱后端（k8s/e2b/opensandbox）或框架将来提供 ``stat_size`` 时，只改这里。

版本锚点：agentscope 2.0.5。``cache_file`` 的 LRU 淘汰逻辑是从框架同名方法
逐行搬过来的（框架把它和 getmtime 写在一起，无法只替换 stat 部分），升级框架时
需对照 ``agentscope/state/_state.py`` 复核。
"""

from __future__ import annotations

import logging
import shlex
from typing import Any, Awaitable, Callable, Optional

from pydantic import Field, PrivateAttr

from agentscope.state import ToolContext
from agentscope.state._state import ReadCacheEntry
from agentscope.tool import BackendBase, LocalBackend

logger = logging.getLogger(__name__)

# (mtime, size)；size 取不到时用 UNKNOWN_SIZE 占位，退化为纯 mtime 比对
Fingerprint = tuple[float, int]
UNKNOWN_SIZE = -1

# 取指纹的函数签名：给绝对路径，返回 (mtime, size)，文件不存在/取不到返回 None
StatFn = Callable[[BackendBase, str], Awaitable[Optional[Fingerprint]]]


class BackendReadCacheEntry(ReadCacheEntry):
    """框架缓存条目 + 指纹里的 size 分量。

    ``updated_at`` 继续存 mtime（沿用父类字段，框架别处读它不会出错），
    size 存在新增字段里。父类字段一个没动，因此条目在框架眼里完全合法。
    """

    fp_size: int = UNKNOWN_SIZE
    """指纹的 size 分量；-1 表示当次没能取到大小（退化为只比 mtime）。"""


async def local_stat(backend: BackendBase, file_path: str) -> Optional[Fingerprint]:
    """本地后端取指纹：backend 与后端进程同一文件系统，直接用它的 stat_mtime。

    ``LocalBackend.stat_mtime`` 就是 ``os.stat(path).st_mtime``，float 秒精度
    （亚秒可辨），与框架原本的行为一致，因此 size 不再额外取。
    """
    mtime = await backend.stat_mtime(file_path)
    if mtime is None:
        return None
    return (float(mtime), UNKNOWN_SIZE)


async def remote_stat(backend: BackendBase, file_path: str) -> Optional[Fingerprint]:
    """沙箱后端取指纹：一次 shell 往返同时拿 mtime 与 size。

    ``stat -c '%Y %s'``（coreutils）失败时退 ``stat -f '%m %z'``（BSD/macOS），
    与框架 ``BackendBase.stat_mtime`` 的兼容策略保持一致。两者都拿不到时退回
    ``backend.stat_mtime``（只有 mtime），保证不比框架更差。
    """
    quoted = shlex.quote(file_path)
    script = (
        f"stat -c '%Y %s' {quoted} 2>/dev/null || "
        f"stat -f '%m %z' {quoted} 2>/dev/null"
    )
    try:
        result = await backend.exec_shell(["sh", "-c", script])
    except Exception as e:  # 后端不可用（容器已回收等）：按"取不到指纹"处理
        logger.debug("[tool_context] stat 执行失败 %s: %s", file_path, e)
        return None
    if result.ok():
        parts = result.stdout.decode("utf-8", errors="replace").split()
        if len(parts) == 2:
            try:
                return (float(parts[0]), int(parts[1]))
            except ValueError:
                pass
    mtime = await backend.stat_mtime(file_path)
    if mtime is None:
        return None
    return (float(mtime), UNKNOWN_SIZE)


class BackendAwareToolContext(ToolContext):
    """走 backend stat 的 ToolContext；沙箱与本地统一路径，行为只在 stat 来源上不同。"""

    read_file_cache: list[BackendReadCacheEntry] = Field(default_factory=list)
    """重声明父类字段以带上 size 分量；语义、上限、淘汰顺序与父类完全一致。"""

    # backend 与 stat_fn 是运行期依赖，不参与序列化（AgentState 落库只存
    # context/summary，本类不会被反序列化重建；即便被重建也只是退回父类行为）
    _backend: Any = PrivateAttr(default=None)
    _stat_fn: Optional[StatFn] = PrivateAttr(default=None)

    def bind_backend(self, backend: Any, stat_fn: Optional[StatFn] = None) -> None:
        """绑定工具执行后端（build 时调用一次，与 Read/Write/Edit 用同一个实例）。

        stat_fn 不传时按后端类型自动选：LocalBackend 走本地 os.stat，
        其余（docker/k8s/e2b/opensandbox 等沙箱后端）走 shell stat。
        """
        self._backend = backend
        self._stat_fn = stat_fn

    async def _fingerprint(self, file_path: str) -> Optional[Fingerprint]:
        """取文件指纹；未绑定 backend 时退回框架原本地 stat 行为。"""
        backend = self._backend
        if backend is None:
            import aiofiles.os

            try:
                mtime = await aiofiles.os.path.getmtime(file_path)
                return (float(mtime), UNKNOWN_SIZE)
            except Exception:
                return None
        stat_fn = self._stat_fn or (
            local_stat if isinstance(backend, LocalBackend) else remote_stat
        )
        try:
            return await stat_fn(backend, file_path)
        except Exception as e:
            logger.debug("[tool_context] 取指纹失败 %s: %s", file_path, e)
            return None

    async def get_cache(self, file_path: str) -> Optional[ReadCacheEntry]:
        """命中且指纹一致才返回；指纹变了（或文件没了）就地淘汰，与父类语义一致。"""
        for entry in self.read_file_cache:
            if entry.file_path != file_path:
                continue
            current = await self._fingerprint(file_path)
            if current is not None and current == (
                entry.updated_at,
                getattr(entry, "fp_size", UNKNOWN_SIZE),
            ):
                return entry
            self.read_file_cache.remove(entry)
            return None
        return None

    async def cache_file(self, file_path: str, lines: list[str]) -> None:
        """缓存文件内容（LRU 淘汰）。逻辑与框架 2.0.5 同名方法一致，只换 stat 来源。"""
        fingerprint = await self._fingerprint(file_path)
        if fingerprint is None:
            # 取不到 mtime（文件不存在/后端不可用），与框架一样跳过缓存
            return
        updated_at, fp_size = fingerprint

        new_entry_bytes = sum(len(line.encode("utf-8")) for line in lines) / 1024

        # 同一文件只保留最新一条
        self.read_file_cache = [
            entry for entry in self.read_file_cache if entry.file_path != file_path
        ]

        # 超出条数上限：淘汰最旧
        while len(self.read_file_cache) >= self.max_cache_files:
            self.read_file_cache.pop(0)

        # 超出字节上限：淘汰最旧
        current_size = sum(entry.bytes for entry in self.read_file_cache)
        while (
            self.read_file_cache
            and current_size + new_entry_bytes > self.max_cache_bytes
        ):
            removed = self.read_file_cache.pop(0)
            current_size -= removed.bytes

        self.read_file_cache.append(
            BackendReadCacheEntry(
                lines=lines,
                updated_at=updated_at,
                bytes=new_entry_bytes,
                file_path=file_path,
                fp_size=fp_size,
            ),
        )


def build_tool_context(
    backend: Any,
    *,
    remote: Optional[bool] = None,
) -> BackendAwareToolContext:
    """按后端类型造一个绑定好的 ToolContext（architectures/base.py 的唯一接线点）。

    Args:
        backend: 工具执行后端，与 Read/Write/Edit/Shell 用的是同一个实例。
        remote: 后端是否运行在**另一个文件系统命名空间**里（沙箱）。None 时按
            ``isinstance(backend, LocalBackend)`` 自动判定；显式传入可覆盖判定，
            便于将来接入非 POSIX 沙箱或本地/远端混合部署。
    """
    ctx = BackendAwareToolContext()
    if remote is None:
        remote = not isinstance(backend, LocalBackend)
    ctx.bind_backend(backend, remote_stat if remote else local_stat)
    logger.info(
        "[tool_context] 文件读缓存 stat 来源=%s backend=%s",
        "remote" if remote else "local",
        type(backend).__name__,
    )
    return ctx
