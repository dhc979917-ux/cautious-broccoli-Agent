"""沙箱文件读缓存（BackendAwareToolContext）回归验证（离线：不连模型、不连库、不起沙箱）。

背景（2026-09-10 会话 276 实测）：docker 沙箱模式下 Edit / 覆盖写 100% 失败，报
"To edit a file, you must first read it using the Read tool."。根因是框架
ToolContext 用 aiofiles.os.path.getmtime 在**后端进程本地**文件系统 stat 沙箱路径
/workspace/...，本地不存在 → cache_file 静默跳过 → get_cache 恒 None。
本次改动把两处 stat 换成走工具自己的 backend（BackendBase.stat_mtime / shell stat）。

用法（任意目录）：
    python test/_test_sandbox_tool_context.py

覆盖三组：
  1. 指纹取值：remote_stat 解析 "%Y %s" / shell 失败退回 stat_mtime / 全失败返 None；
     local_stat 用 backend.stat_mtime；build_tool_context 按后端类型自动选并可显式覆盖。
  2. 缓存语义：沙箱路径能缓存并命中（原 bug 的正向断言）/ mtime 变了失效 /
     **同一秒只改大小也能识别为失效**（秒级 mtime 的陈旧缓存防护）/ 文件消失即淘汰 /
     LRU 条数与字节上限 / 继承的 clean_file_cache 不丢子类字段。
  3. 端到端（真实框架 Read/Write/Edit + 假沙箱后端）：Read→Edit 成功、Read→覆盖写成功；
     同样的流程换成框架原生 ToolContext 必须失败（证明测试确实咬住了这个 bug）。
"""
import asyncio
import os
import sys
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from agentscope.state import AgentState, ToolContext  # noqa: E402
from agentscope.tool import Edit, Read, Write  # noqa: E402
from agentscope.tool._builtin._backend import ExecResult, LocalBackend  # noqa: E402

from app.agents.sandbox_tool_context import (  # noqa: E402
    UNKNOWN_SIZE,
    BackendAwareToolContext,
    BackendReadCacheEntry,
    build_tool_context,
    local_stat,
    remote_stat,
)

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' -> ' + str(extra)) if extra else ''}")


# ==================== 假沙箱后端 ====================
class FakeSandboxBackend:
    """内存文件系统的沙箱后端替身：路径在"沙箱"里存在，在后端本地不存在。

    故意不继承 LocalBackend —— build_tool_context 的自动判定必须把它认成远端。
    mtime 只有秒级精度（复刻 docker 内 `stat -c %Y` 的真实粒度）。
    """

    def __init__(self, mtime_granularity: str = "second"):
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, float] = {}
        self._clock = 1_700_000_000.0
        self._granularity = mtime_granularity
        self.shell_calls: list[list[str]] = []
        self.broken_shell = False

    # --- 时钟：默认整秒推进，用于复刻沙箱侧秒级 mtime ---
    def tick(self, seconds: float = 1.0) -> None:
        self._clock += seconds

    def _now(self) -> float:
        return (
            float(int(self._clock))
            if self._granularity == "second"
            else self._clock
        )

    # --- BackendBase 接口 ---
    def isabs(self, path: str) -> bool:
        return str(path).startswith("/")

    async def file_exists(self, path: str) -> bool:
        return path in self.files

    async def is_dir(self, path: str) -> bool:
        return False

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        # 每次落盘推进 1 秒：复刻真实场景里两次工具调用之间隔着一次 LLM 往返，
        # 需要"同一秒改写"时测试自己直接改 self.files 即可（见 group_cache）。
        self.tick(1.0)
        self.files[path] = data
        self.mtimes[path] = self._now()

    async def stat_mtime(self, path: str) -> Optional[float]:
        return self.mtimes.get(path)

    async def exec_shell(self, command, *, cwd=None, timeout=None) -> ExecResult:
        self.shell_calls.append(list(command))
        if self.broken_shell:
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"backend down")
        script = command[-1] if command[:2] == ["sh", "-c"] else " ".join(command)
        if script.startswith("mkdir"):
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        if script.startswith("stat "):
            path = script.split(" 2>/dev/null")[0].split()[-1].strip("'\"")
            if path in self.files:
                out = f"{int(self.mtimes[path])} {len(self.files[path])}\n"
                return ExecResult(exit_code=0, stdout=out.encode(), stderr=b"")
            return ExecResult(exit_code=1, stdout=b"", stderr=b"No such file")
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")


class FakeLocalBackend(LocalBackend):
    """真 LocalBackend 子类，只把 stat_mtime 换成受控值（避免依赖真实文件系统）。"""

    def __init__(self, mtime: Optional[float] = 123.456):
        super().__init__()
        self._mtime = mtime

    async def stat_mtime(self, path: str) -> Optional[float]:
        return self._mtime


def text_of(chunk) -> str:
    return "".join(
        getattr(b, "text", "") or "" for b in (getattr(chunk, "content", None) or [])
    )


# ==================== 1. 指纹取值 ====================
async def group_stat():
    print("\n[1] 指纹取值（stat 来源）")

    sb = FakeSandboxBackend()
    await sb.write_file("/workspace/a.py", b"print(1)\n")

    fp = await remote_stat(sb, "/workspace/a.py")
    check("remote_stat 一次 shell 往返拿到 (mtime, size)",
          fp == (float(int(sb.mtimes["/workspace/a.py"])), 9), fp)
    check("remote_stat 走的是 sh -c stat",
          any(c[:2] == ["sh", "-c"] and "stat -c '%Y %s'" in c[-1] for c in sb.shell_calls),
          sb.shell_calls[-1] if sb.shell_calls else None)

    check("remote_stat 文件不存在返回 None",
          await remote_stat(sb, "/workspace/nope.py") is None)

    sb.broken_shell = True
    fp_fb = await remote_stat(sb, "/workspace/a.py")
    check("remote_stat shell 失败时退回 backend.stat_mtime（size 未知）",
          fp_fb == (float(int(sb.mtimes["/workspace/a.py"])), UNKNOWN_SIZE), fp_fb)
    sb.broken_shell = False

    async def boom(backend, path):
        raise RuntimeError("backend exploded")

    sb.exec_shell = boom
    check("remote_stat 后端抛异常时返回 None（不冒泡）",
          await remote_stat(sb, "/workspace/a.py") is None)

    lb = FakeLocalBackend(mtime=123.456)
    check("local_stat 用 backend.stat_mtime（float 精度，不额外取 size）",
          await local_stat(lb, "C:/x/a.py") == (123.456, UNKNOWN_SIZE))
    lb2 = FakeLocalBackend(mtime=None)
    check("local_stat 文件不存在返回 None", await local_stat(lb2, "C:/x/a.py") is None)

    ctx_r = build_tool_context(FakeSandboxBackend())
    ctx_l = build_tool_context(FakeLocalBackend())
    check("build_tool_context 自动判定：非 LocalBackend -> remote",
          ctx_r._stat_fn is remote_stat)
    check("build_tool_context 自动判定：LocalBackend 子类 -> local",
          ctx_l._stat_fn is local_stat)
    check("build_tool_context 支持显式覆盖 remote",
          build_tool_context(FakeLocalBackend(), remote=True)._stat_fn is remote_stat)
    check("build_tool_context 返回的是 ToolContext 子类（框架其它代码无感）",
          isinstance(ctx_r, ToolContext) and isinstance(ctx_r, BackendAwareToolContext))


# ==================== 2. 缓存语义 ====================
async def group_cache():
    print("\n[2] 缓存语义")

    sb = FakeSandboxBackend()
    path = "/workspace/outputs/gen.py"
    await sb.write_file(path, b"line1\nline2\n")
    ctx = build_tool_context(sb)

    # 原 bug：沙箱路径在后端本地不存在，框架 ToolContext 缓存不进去
    base_ctx = ToolContext()
    await base_ctx.cache_file(path, ["line1\n", "line2\n"])
    check("对照组：框架原生 ToolContext 在沙箱路径上缓存不进去（复现原 bug）",
          base_ctx.read_file_cache == [] and await base_ctx.get_cache(path) is None)

    await ctx.cache_file(path, ["line1\n", "line2\n"])
    hit = await ctx.get_cache(path)
    check("沙箱路径能缓存并命中（原 bug 的正向断言）",
          hit is not None and hit.lines == ["line1\n", "line2\n"], hit)
    check("缓存条目带 size 指纹", isinstance(hit, BackendReadCacheEntry) and hit.fp_size == 12,
          getattr(hit, "fp_size", None))

    # mtime 变化 -> 失效
    sb.tick(5)
    await sb.write_file(path, b"line1\nline2\nline3\n")
    check("mtime 变化后缓存失效", await ctx.get_cache(path) is None)
    check("失效条目被就地淘汰", ctx.read_file_cache == [])

    # 同一秒内只改大小（docker mtime 秒级粒度的陈旧缓存防护）
    await ctx.cache_file(path, ["line1\n", "line2\n", "line3\n"])
    same_second = b"xxxxx\n"  # 长度不同，时钟不推进 -> mtime 不变
    sb.files[path] = same_second
    check("同一秒内改写（mtime 不变、size 变）也能识别为失效",
          await ctx.get_cache(path) is None,
          (sb.mtimes[path], len(same_second)))

    # 文件消失 -> 淘汰
    await sb.write_file(path, b"line1\n")
    await ctx.cache_file(path, ["line1\n"])
    del sb.files[path]
    sb.mtimes.pop(path, None)
    check("文件消失后返回 None 并淘汰条目",
          await ctx.get_cache(path) is None and ctx.read_file_cache == [])

    check("未缓存过的文件返回 None", await ctx.get_cache("/workspace/other.py") is None)

    # LRU：条数上限
    sb2 = FakeSandboxBackend()
    ctx2 = build_tool_context(sb2)
    ctx2.max_cache_files = 3
    for i in range(4):
        p = f"/workspace/f{i}.txt"
        await sb2.write_file(p, b"x")
        await ctx2.cache_file(p, ["x"])
    check("LRU 条数上限生效（淘汰最旧）",
          [e.file_path for e in ctx2.read_file_cache]
          == ["/workspace/f1.txt", "/workspace/f2.txt", "/workspace/f3.txt"],
          [e.file_path for e in ctx2.read_file_cache])

    # LRU：字节上限
    sb3 = FakeSandboxBackend()
    ctx3 = build_tool_context(sb3)
    ctx3.max_cache_bytes = 10001  # 字段约束 gt=10000
    big = "y" * (3 * 1024 * 1024)  # 每条 3072KB（框架的 bytes 单位是 KB）
    for i in range(4):
        p = f"/workspace/big{i}.txt"
        await sb3.write_file(p, big.encode())
        await ctx3.cache_file(p, [big])
    total = sum(e.bytes for e in ctx3.read_file_cache)
    check("LRU 字节上限生效（超限的最旧条目被淘汰）",
          total <= ctx3.max_cache_bytes
          and [e.file_path for e in ctx3.read_file_cache]
          == ["/workspace/big1.txt", "/workspace/big2.txt", "/workspace/big3.txt"],
          f"{len(ctx3.read_file_cache)} 条 / {total:.0f}KB")
    del big

    # 同一路径重复缓存只留最新一条
    sb4 = FakeSandboxBackend()
    ctx4 = build_tool_context(sb4)
    await sb4.write_file("/workspace/dup.txt", b"a")
    await ctx4.cache_file("/workspace/dup.txt", ["a"])
    sb4.tick(2)
    await sb4.write_file("/workspace/dup.txt", b"b")
    await ctx4.cache_file("/workspace/dup.txt", ["b"])
    check("同一路径重复缓存只留最新一条",
          len(ctx4.read_file_cache) == 1 and ctx4.read_file_cache[0].lines == ["b"])

    # 继承的 clean_file_cache 不丢子类字段
    await ctx4.clean_file_cache({"/workspace/dup.txt"})
    check("clean_file_cache 保留命中条目且仍是子类实例",
          len(ctx4.read_file_cache) == 1
          and isinstance(ctx4.read_file_cache[0], BackendReadCacheEntry))
    await ctx4.clean_file_cache({"/workspace/other.txt"})
    check("clean_file_cache 淘汰未保留条目", ctx4.read_file_cache == [])

    # 未绑定 backend：退回框架原本地 stat（不至于比现状更差）
    naked = BackendAwareToolContext()
    await naked.cache_file("/definitely/not/here.py", ["x"])
    check("未绑定 backend 时本地 stat 失败则跳过缓存（与框架一致）",
          naked.read_file_cache == [])


# ==================== 3. 端到端：真实框架 Read/Write/Edit ====================
async def group_e2e():
    print("\n[3] 端到端（真实 Read/Write/Edit + 假沙箱后端）")

    path = "/workspace/outputs/gen_ppt.py"

    async def run(tool_context_factory):
        sb = FakeSandboxBackend()
        await sb.write_file(path, b"from pptx import Presentation\nCOLOR = 1\n")
        state = AgentState(session_id="276")
        state.tool_context = tool_context_factory(sb)
        state.tool_context.activated_groups = []
        read, edit, write = Read(backend=sb), Edit(backend=sb), Write(backend=sb)

        r1 = await read.call(file_path=path, offset=1, limit=100, _agent_state=state)
        r2 = await edit.call(
            file_path=path, old_string="COLOR = 1", new_string="COLOR = 2",
            _agent_state=state,
        )
        disk_after_edit = sb.files.get(path)
        # 覆盖写已存在文件：必须先 Read 才允许
        r3 = await read.call(file_path=path, offset=1, limit=100, _agent_state=state)
        r4 = await write.call(file_path=path, content="COLOR = 3\n", _agent_state=state)
        return sb, state, [r1, r2, r3, r4], disk_after_edit

    sb, state, chunks, disk_after_edit = await run(build_tool_context)
    check("Read 正常返回内容", "COLOR = 1" in text_of(chunks[0]))
    check("Read 后 Edit 成功（原来是 must first read）",
          chunks[1].state.name != "ERROR" and "Successfully replaced" in text_of(chunks[1]),
          text_of(chunks[1])[:120])
    check("Edit 落盘内容正确（基于刚读到的内容替换）",
          disk_after_edit == b"from pptx import Presentation\nCOLOR = 2\n", disk_after_edit)
    check("Read 后覆盖写已存在文件成功",
          chunks[3].state.name != "ERROR" and "written successfully" in text_of(chunks[3]),
          text_of(chunks[3])[:120])
    check("覆盖写真的落盘", sb.files[path] == b"COLOR = 3\n", sb.files[path])
    check("activated_groups 仍可正常赋值", state.tool_context.activated_groups == [])

    # Edit 落盘后 mtime 变化 -> 缓存自动失效，下一次 Edit 仍要求重新 Read（护栏没被削弱）
    again = await Edit(backend=sb).call(
        file_path=path, old_string="COLOR = 3", new_string="COLOR = 4", _agent_state=state,
    )
    check("写后缓存失效：不重新 Read 直接 Edit 仍被拦（护栏未被削弱）",
          again.state.name == "ERROR" and "must first read" in text_of(again),
          text_of(again)[:100])

    # 对照组：框架原生 ToolContext 在同样的沙箱后端上必然失败
    sb_b, _st_b, chunks_b, _disk_b = await run(lambda _sb: ToolContext())
    check("对照组：框架原生 ToolContext 下 Edit 报 must first read（咬住原 bug）",
          chunks_b[1].state.name == "ERROR" and "must first read" in text_of(chunks_b[1]),
          text_of(chunks_b[1])[:100])
    check("对照组：框架原生 ToolContext 下覆盖写同样被拒",
          chunks_b[3].state.name == "ERROR" and "must read the file first" in text_of(chunks_b[3]),
          text_of(chunks_b[3])[:100])


async def main():
    await group_stat()
    await group_cache()
    await group_e2e()

    failed = [n for n, ok in results if not ok]
    print(f"\n===== 合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)} =====")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
