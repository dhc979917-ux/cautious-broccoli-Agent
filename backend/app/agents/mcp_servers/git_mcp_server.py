"""本地 Git MCP server（stdio）：把本机 git 能力以只读检视工具暴露给 Agent。

定位：补齐六件套里缺失的 git 工具。先只提供只读检视类命令（status/log/diff/
show/branch/blame 摘要），写操作（add/commit/push）不进第一版——Agent 直接写
仓库风险高，等沙箱与权限体系（harness 第 3 步）就位后再评估放开。

注册方式：管理端 MCP 页面新增 stdio 服务，command=python，
args=["<backend>/app/agents/mcp_servers/git_mcp_server.py"]。
所有命令用参数列表方式调用 git（不经 shell），输出统一截断防上下文爆炸。
"""
import os
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("git")

# 与 backend 工具输出截断同策略：保头尾、省中间
_HEAD = 6000
_TAIL = 2000

# git 子进程环境：禁交互提示、禁 pager。防止在 MCP stdio 传输下因等待
# 终端输入/分页器而挂起（此时服务端 stdin 是 JSON-RPC 管道，无终端）。
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
}


def _truncate(text: str) -> str:
    if len(text) <= _HEAD + _TAIL:
        return text
    omitted = len(text) - _HEAD - _TAIL
    return text[:_HEAD] + f"\n…[中间省略 {omitted} 字符]…\n" + text[-_TAIL:]


def _git(repo: str, *args: str) -> str:
    repo = os.path.abspath(os.path.expanduser(repo or "."))
    if not os.path.isdir(repo):
        return f"Error: 仓库目录不存在: {repo}"
    try:
        result = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=repo,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=_GIT_ENV,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return "Error: 未找到 git 可执行文件，请确认 git 已安装并在 PATH 中。"
    except subprocess.TimeoutExpired:
        return "Error: git 命令超时（60s）。"
    out = (result.stdout or "") + (("\n[stderr]\n" + result.stderr) if result.returncode != 0 and result.stderr else "")
    if result.returncode != 0 and not out.strip():
        out = f"Error: git 退出码 {result.returncode}"
    return _truncate(out.strip() or "(无输出)")


@mcp.tool()
def git_status(repo: str = ".") -> str:
    """查看 git 仓库工作区状态（改动文件、暂存情况、当前分支）。

    Args:
        repo: 仓库目录的绝对路径，默认当前目录。
    """
    return _git(repo, "status", "--short", "--branch")


@mcp.tool()
def git_log(repo: str = ".", n: int = 20, path: str = "") -> str:
    """查看提交历史（单行摘要 + 作者 + 日期）。

    Args:
        repo: 仓库目录的绝对路径。
        n: 返回最近多少条提交（最大 100）。
        path: 可选，只看某个文件/目录相关的提交。
    """
    n = max(1, min(int(n or 20), 100))
    args = ["log", f"-{n}", "--pretty=format:%h %ad %an: %s", "--date=short"]
    if path:
        args += ["--", path]
    return _git(repo, *args)


@mcp.tool()
def git_diff(repo: str = ".", ref: str = "", path: str = "") -> str:
    """查看差异。ref 为空 = 工作区未暂存改动；'--staged' = 已暂存改动；
    也可以是分支/提交（如 'HEAD~3' 或 'main'）。

    Args:
        repo: 仓库目录的绝对路径。
        ref: 空字符串 / '--staged' / 分支名 / 提交号。
        path: 可选，限定某个文件/目录。
    """
    args = ["diff"]
    if ref and ref != "--staged":
        args.append(ref)
    args.append("--stat=200")
    if ref == "--staged":
        args.insert(1, "--staged")
    if path:
        args += ["--", path]
    return _git(repo, *args)


@mcp.tool()
def git_show(repo: str = ".", ref: str = "HEAD", path: str = "") -> str:
    """查看某个提交的完整改动（默认 HEAD）。

    Args:
        repo: 仓库目录的绝对路径。
        ref: 提交号/分支/tag。
        path: 可选，只看提交中某个文件的改动。
    """
    args = ["show", ref or "HEAD", "--stat=200", "-p"]
    if path:
        args += ["--", path]
    return _git(repo, *args)


@mcp.tool()
def git_branch(repo: str = ".", all_branches: bool = False) -> str:
    """列出分支及最近提交。

    Args:
        repo: 仓库目录的绝对路径。
        all_branches: True 时包含远程分支。
    """
    args = ["branch", "-v", "--no-color"]
    if all_branches:
        args.append("-a")
    return _git(repo, *args)


if __name__ == "__main__":
    mcp.run()
