"""工具层守卫（AgentScope ToolMiddleware）：省 token 与读取纪律。

背景（traces.jsonl 实测）：曾出现同一个 637KB 的 BMP 被 Read 工具完整读 7 次、
单个任务累计 input 235 万 token 的案例。二进制按 utf-8(errors=replace) 解码
只会产生乱码，却以 tool_result 形式反复进入模型上下文。

- ReadGuardMiddleware（挂 Read）：
  1) 拒读二进制文件（图片/压缩包/音视频/可执行/Office 二进制/pdf 等），
     直接返回引导文案，避免乱码灌进上下文；
  2) 同一文件被重复整读时追加提示，引导用 offset/limit 或 Grep 精确定位。
- WriteTrackMiddleware（挂 Write/Edit）：
  文件被修改后重置其读取计数，保证"写后读回验证"的合法重读不受干扰。

tracker 实例在 architectures/base.py 的 build() 里创建并由两个中间件共享；
工具实例随 Agent 按会话构建，守卫状态天然会话隔离。
"""

import logging
import os
import re
import shlex
from typing import Any, AsyncGenerator, Callable

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool._base import ToolBase, ToolMiddlewareBase
from agentscope.tool._response import ToolChunk
from agentscope.tool import BackendBase, Edit, Write
from agentscope.state import AgentState

logger = logging.getLogger(__name__)

# 拒读名单：这些扩展名的文件 Read 只会解出乱码（框架 read_file 是裸读字节）。
# 注意：.svg/.csv/.json/.html 是文本，不在此列。
BINARY_EXTENSIONS = {
    # 图片
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".tif", ".tiff", ".heic", ".psd",
    # 压缩包
    ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz",
    # Office 二进制（docx/xlsx/pptx 实为 zip 包，同样不可读）
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".docm", ".xlsm", ".pptm",
    # 文档（框架 Read 无 pdf 解析器，读出来是乱码）
    ".pdf",
    # 音视频
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flac",
    ".m4a", ".wmv",
    # 可执行/库
    ".exe", ".dll", ".so", ".pyd", ".msi", ".bin",
    # 数据库文件
    ".db", ".sqlite", ".sqlite3",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2",
    # 编译产物
    ".pyc", ".pyo", ".class", ".o", ".obj",
}

_BINARY_REJECT_TEMPLATE = (
    "Error: '{path}' 是二进制文件（{ext}），Read 按文本读取只会产生乱码并"
    "浪费大量 token，已拒绝读取。请改用正确方式处理：图片用 PowerShell + "
    "Python(PIL/numpy) 或对应技能处理，文档/压缩包等调用对应技能脚本。"
    "不要再用 Read 读取此文件。"
)

_REREAD_HINT = (
    "\n[提示：该文件此前已被完整读取过 {n} 次，内容大概率仍在上下文中。"
    "请优先用 Grep 定位所需片段，或用 Read 的 offset/limit 参数分页读取，"
    "避免重复整读浪费 token。]\n"
)


def _norm(path: str) -> str:
    """路径归一化（Windows 大小写不敏感 + 统一分隔符），用于计数去重。"""
    return os.path.normcase(os.path.normpath(str(path or "")))


class FileAccessTracker:
    """记录会话内文件被 Read 的次数（Write/Edit 后重置）。"""

    def __init__(self) -> None:
        self._read_counts: dict[str, int] = {}

    def count_and_mark_read(self, path: str) -> int:
        """返回本次之前已读次数，并把计数 +1。"""
        key = _norm(path)
        prev = self._read_counts.get(key, 0)
        self._read_counts[key] = prev + 1
        return prev

    def reset(self, path: str) -> None:
        self._read_counts.pop(_norm(path), None)


class ReadGuardMiddleware(ToolMiddlewareBase):
    """Read 守卫：拒读二进制 + 重复整读提示。"""

    def __init__(self, tracker: FileAccessTracker) -> None:
        self._tracker = tracker

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        file_path = str(input_kwargs.get("file_path") or "")
        ext = os.path.splitext(file_path)[1].lower()

        # 1) 二进制拒读：短路返回，不执行真实读取
        if ext in BINARY_EXTENSIONS:
            yield ToolChunk(
                content=[TextBlock(text=_BINARY_REJECT_TEMPLATE.format(
                    path=file_path, ext=ext,
                ))],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        prev_reads = self._tracker.count_and_mark_read(file_path)

        # 2) 首次读取：直接透传
        if prev_reads == 0:
            async for chunk in next_handler(**input_kwargs):
                yield chunk
            return

        # 3) 重复读取：透传内容，但在首个文本块前追加提示
        hint = _REREAD_HINT.format(n=prev_reads)
        hinted = False
        async for chunk in next_handler(**input_kwargs):
            if not hinted and chunk.content:
                first = chunk.content[0]
                if isinstance(first, TextBlock):
                    chunk.content[0] = TextBlock(text=hint + first.text)
                    hinted = True
            yield chunk


class WriteTrackMiddleware(ToolMiddlewareBase):
    """Write/Edit 守卫：文件被修改后重置读取计数（合法重读不受干扰）。"""

    def __init__(self, tracker: FileAccessTracker) -> None:
        self._tracker = tracker

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        file_path = str(input_kwargs.get("file_path") or "")
        async for chunk in next_handler(**input_kwargs):
            yield chunk
        # 写/编辑完成后重置（即使失败也重置，无副作用）
        if file_path:
            self._tracker.reset(file_path)


# ======================================================================
# WriteSkipReadCheck：跳过 Write 的"先 Read 再 Write"强制检查
# ======================================================================
# 背景：AgentScope 内置 Write 工具在覆盖已存在文件时，强制要求先 Read
# 才能写（安全设计）。但本平台 Agent 按会话/按轮重建，重建后 Read 缓存
# 丢失，导致第二轮覆盖第一轮的文件时被拦截 → 被迫先 Read（把整个文件
# 内容灌进上下文，浪费 ~4500 token）→ 再 Write。
#
# 解决：在 Write.call 之前预填充 Read 缓存，让 Write 的检查通过，
# 无需额外 Read 调用。只在文件已存在且缓存为空时触发，不影响新建文件。

class WriteSkipReadCheck(Write):
    """Write 子类：覆盖已存在文件时自动预填充 Read 缓存。

    与原版 Write 行为完全一致，唯一区别是：当目标文件已存在且
    tool_context 中没有该文件的 Read 缓存时，先读取文件内容并填充
    缓存，使 Write 的"先 Read 再 Write"检查通过，避免被迫多一次 Read。
    """

    async def call(  # type: ignore[override]
        self,
        file_path: str,
        content: str,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        # 只在文件已存在、有 agent_state、且缓存为空时预填充
        if _agent_state is not None:
            try:
                tc = getattr(_agent_state, "tool_context", None)
                if tc is not None:
                    existing = await tc.get_cache(file_path)
                    if existing is None and await self._backend.file_exists(file_path):
                        try:
                            raw = await self._backend.read_file(file_path)
                            text = raw.decode("utf-8", errors="replace")
                            lines = text.splitlines(keepends=True)
                            await tc.cache_file(file_path, lines)
                            logger.debug(
                                "[write_skip_read] 预填充 %s 的 Read 缓存，跳过冗余 Read",
                                file_path,
                            )
                        except Exception as e:
                            logger.debug("[write_skip_read] 预填充失败（忽略）: %s", e)
            except Exception as e:
                logger.debug("[write_skip_read] 缓存检查失败（忽略）: %s", e)
        result = await super().call(file_path, content, _agent_state)
        if result.state != ToolResultState.ERROR and _agent_state is not None:
            await _refresh_read_cache(_agent_state, self._backend, file_path, content)
        return result


async def _refresh_read_cache(
    agent_state: AgentState,
    backend: BackendBase,
    file_path: str,
    content: str | None = None,
) -> None:
    try:
        if content is None:
            content = (await backend.read_file(file_path)).decode('utf-8', errors='replace')
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        await agent_state.tool_context.cache_file(file_path, content.splitlines(keepends=True))
    except Exception as error:
        logger.warning(
            '[read_cache] refresh failed for %s: %s', file_path, type(error).__name__,
        )


class EditWithReadCache(Edit):
    async def call(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        _agent_state: AgentState | None = None,
    ) -> ToolChunk:
        result = await super().call(
            file_path, old_string, new_string, replace_all, _agent_state,
        )
        if result.state != ToolResultState.ERROR and _agent_state is not None:
            await _refresh_read_cache(_agent_state, self._backend, file_path)
        return result


# ======================================================================
# PowerShell 危险命令守卫（harness 第 3 步：执行沙箱化·第一层）
# 背景：PowerShell 工具在宿主机裸跑（无隔离），曾出现 Agent 可触达全盘的情况。
# 真正的隔离靠容器（Docker/OpenSandbox，后续评估）；在此之前用黑名单收口，
# 拦截不可逆/系统级操作。黑名单是防御纵深，不是边界——边界靠沙箱。
# ======================================================================

# 硬黑名单：无论参数如何都拒绝（系统级、不可逆、与业务任务无关）
_SHELL_HARD_BLOCK: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bformat-volume\b|\bclear-disk\b|\binitialize-disk\b|"
                r"\bremove-partition\b|\bnew-partition\b|\bdiskpart\b", re.I),
     "磁盘/分区操作"),
    (re.compile(r"\bshutdown\b|\brestart-computer\b|\bstop-computer\b", re.I),
     "关机/重启"),
    (re.compile(r"\bset-executionpolicy\b", re.I),
     "修改 PowerShell 执行策略"),
    (re.compile(r"\binvoke-expression\b|\biex\s|^\s*iex\s*$", re.I),
     "动态代码执行 Invoke-Expression"),
    (re.compile(r"\bdownloadstring\b|\bdownloadfile\b", re.I),
     "下载并执行远程脚本"),
    (re.compile(r"\breg\s+(add|delete|import|restore)\b", re.I),
     "注册表写/删"),
    (re.compile(r"\bnet\s+(user|localgroup)\b|\bnew-localuser\b|"
                r"\bremove-localuser\b|\bset-localuser\b", re.I),
     "系统账户管理"),
    (re.compile(r"\bschtasks\s+/(create|delete)\b", re.I),
     "计划任务创建/删除"),
    (re.compile(r"\bnew-service\b|\bremove-service\b|\bsc\.exe\s+(create|delete)\b|"
                r"(?<!\w)sc\s+(create|delete)\b", re.I),
     "系统服务创建/删除"),
    (re.compile(r"\btakeown\b|\bicacls\b|\bcacls\b", re.I),
     "文件所有权/ACL 修改"),
    (re.compile(r"\bvssadmin\b.*\bdelete\b|\bbcdedit\b", re.I),
     "卷影副本/启动配置修改"),
    (re.compile(r"currentversion\\run|start\s*menu\\programs\\startup|"
                r"shell:startup", re.I),
     "开机自启项写入"),
    (re.compile(r"\bclear-recyclebin\b", re.I),
     "清空回收站"),
    (re.compile(r"\bset-mppreference\b.*-disable", re.I),
     "关闭 Defender 防护"),
    (re.compile(r"\\device\\harddisk|\\\\\.\\physicaldrive|\\device\\cdrom", re.I),
     "磁盘设备直写"),
]

# Linux 硬黑名单（服务器 Linux 部署 / docker 沙箱模式生效；
# 上方 Windows 条目在 Linux 下天然不命中，两份合并为纵深防御）
_LINUX_HARD_BLOCK: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdd\s+[^|;&\n]*of=/dev/|\bmkfs\b|\bfdisk\b|\bparted\b|\bsfdisk\b", re.I),
     "磁盘设备/文件系统操作"),
    (re.compile(r"\breboot\b|\bhalt\b|\bpoweroff\b|\binit\s+[06]\b", re.I),
     "关机/重启"),
    (re.compile(r"\buseradd\b|\buserdel\b|\busermod\b|\bpasswd\b|\bgroupadd\b|\bgroupdel\b", re.I),
     "系统账户管理"),
    (re.compile(r"\bsystemctl\s+(stop|disable|mask|reset-failed)\b|\bservice\s+\S+\s+stop\b", re.I),
     "停止/禁用系统服务"),
    (re.compile(r"\biptables\b|\bnft\s|\bufw\s+(disable|delete)\b", re.I),
     "防火墙修改"),
    (re.compile(r"\bcrontab\s+-r\b|\bat\s+-r\b", re.I),
     "计划任务清除"),
    (re.compile(r"\bchmod\s+(-[a-z]+\s+)*777\s+/(\s|$)", re.I),
     "根目录权限放开"),
    (re.compile(r"curl[^|;&\n]*\|\s*(ba)?sh\b|wget[^|;&\n]*\|\s*(ba)?sh\b", re.I),
     "下载并执行远程脚本"),
    (re.compile(r">\s*/dev/(sd[a-z]|nvme|mem\b|kmem\b)", re.I),
     "设备直写"),
    (re.compile(r"\bmount\s|\bumount\s|\bswapon\b|\bswapoff\b|\binsmod\b|\brmmod\b|\bmodprobe\b", re.I),
     "挂载/内核模块操作"),
    (re.compile(r"\bkill\s+(-\w+\s+)*1(\s|$)", re.I),
     "终止 init 进程"),
]

# 合并黑名单：ShellGuardMiddleware 统一遍历（Windows 条目在 Linux 不命中，反之亦然）
_ALL_HARD_BLOCK = _SHELL_HARD_BLOCK + _LINUX_HARD_BLOCK


def _recursive_delete_allowed(command: str, outputs_root: str, cwd: str) -> bool:
    if re.search(r"[;&|<>`$()\n*?~{}]", command):
        return False
    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    if not args or args[0].lower() not in ("rm", "rmdir", "remove-item", "rd", "del"):
        return False
    targets = [arg.strip('"') for arg in args[1:] if not arg.startswith(("-", "/"))]
    if os.name != "nt":
        targets = [arg for arg in args[1:] if not arg.startswith("-")]
    if os.name == "nt" and any(arg.startswith("/") and arg.lower() not in ("/s", "/q") for arg in args[1:]):
        return False
    if not targets:
        return False
    root = os.path.normcase(os.path.realpath(outputs_root))
    try:
        for target in targets:
            path = os.path.normcase(os.path.realpath(os.path.join(cwd, target)))
            if path == root or os.path.commonpath((root, path)) != root:
                return False
    except (OSError, ValueError):
        return False
    return True
# 递归删除：只允许作用于 outputs 目录内（会话产物清理是合法场景）
_SHELL_RECURSIVE_DELETE = re.compile(
    r"\bremove-item\b[^|;&\n]*\s-(r|recurse)\b"
    r"|\brmdir\b[^|;&\n]*\s-(r|recurse)\b"
    r"|\brm\s+(-[a-z]*r[a-z]*\s|--recursive\b)"
    r"|\brd\s+/s\b|\bdel\s+/s\b|\bdeltree\b",
    re.I,
)

_SHELL_BLOCK_TEMPLATE = (
    "Error: 命令被安全策略拦截（{reason}）。当前执行环境未做隔离，"
    "系统级/不可逆操作一律禁止。请改用 outputs 目录内的文件操作完成目标，"
    "或向用户说明该操作需要人工执行。不要重试同类命令。"
)


class ShellGuardMiddleware(ToolMiddlewareBase):
    """PowerShell 守卫：危险命令黑名单 + 递归删除范围限制。"""

    def __init__(self, outputs_root: str, cwd: str | None = None) -> None:
        # outputs_root：允许递归删除的唯一区域（产物目录，可随意清理）
        self._outputs_root = _norm(outputs_root)
        self._cwd = _norm(cwd or outputs_root)

    async def on_tool_call(
        self,
        tool: ToolBase,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        command = str(input_kwargs.get("command") or "")

        for pattern, reason in _ALL_HARD_BLOCK:
            if pattern.search(command):
                yield ToolChunk(
                    content=[TextBlock(text=_SHELL_BLOCK_TEMPLATE.format(reason=reason))],
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return

        if _SHELL_RECURSIVE_DELETE.search(command) and not _recursive_delete_allowed(command, self._outputs_root, self._cwd):
            yield ToolChunk(
                content=[TextBlock(text=_SHELL_BLOCK_TEMPLATE.format(
                    reason="递归删除仅限 outputs 产物目录",
                ))],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        async for chunk in next_handler(**input_kwargs):
            yield chunk
