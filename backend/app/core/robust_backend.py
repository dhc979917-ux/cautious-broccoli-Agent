"""修复 Windows 下 AgentScope 内置工具无法执行子进程的 LocalBackend 子类。

根因：uvicorn 以 --reload 启动时（uvicorn/loops/asyncio.py），在 Windows 上会
强制把事件循环策略设为 WindowsSelectorEventLoopPolicy；而 SelectorEventLoop
在 Windows 上不支持 asyncio 子进程，asyncio.create_subprocess_exec 会抛
NotImplementedError（无错误信息），导致 PowerShell 等工具全部失败且报空错误。

修复：exec_shell 改用同步 subprocess.run，通过 asyncio.to_thread 放到线程池
执行，不依赖事件循环的子进程能力，Selector/Proactor 事件循环下均可用。
同时捕获所有异常并追加写入 traces/subprocess_debug.log，保证错误信息不为空。
"""

import asyncio
import os
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

from agentscope.tool._builtin._backend import (
    LocalBackend,
    ExecResult,
    _subprocess_creation_kwargs,
)

# 调试日志：backend/traces/subprocess_debug.log
_DEBUG_LOG = Path(__file__).resolve().parent.parent.parent / "traces" / "subprocess_debug.log"


class RobustLocalBackend(LocalBackend):
    """LocalBackend 修复版：同步 subprocess + 全异常兜底。"""

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        def _run_sync() -> ExecResult:
            kwargs = _subprocess_creation_kwargs()
            # 注入 UTF-8 编码环境：Windows 控制台默认 GBK，Python 子进程输出中文
            # 会抛 UnicodeEncodeError（calculator 等技能曾因此连败数轮浪费推理轮数）
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    cwd=cwd,
                    timeout=timeout,
                    env=env,
                    **kwargs,
                )
                return ExecResult(
                    exit_code=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            except subprocess.TimeoutExpired:
                # 与原生 LocalBackend 的超时约定保持一致（exit_code=-1 + b"timed out"）
                return ExecResult(exit_code=-1, stdout=b"", stderr=b"timed out")
            except Exception as exc:
                self._log_error("subprocess.run", command, cwd, exc)
                return ExecResult(
                    exit_code=127,
                    stdout=b"",
                    stderr=self._exc_bytes(exc),
                )

        try:
            return await asyncio.to_thread(_run_sync)
        except Exception as exc:
            self._log_error("to_thread", command, cwd, exc)
            return ExecResult(exit_code=-1, stdout=b"", stderr=self._exc_bytes(exc))

    @staticmethod
    def _exc_bytes(exc: BaseException) -> bytes:
        """异常转 bytes，用 repr 保证永不为空（str(exc) 可能是空串）。"""
        msg = f"{type(exc).__name__}: {exc!r}"
        return msg.encode("utf-8", errors="replace")

    @staticmethod
    def _log_error(stage: str, command: list[str], cwd: str | None, exc: BaseException) -> None:
        """把异常详情追加写入调试日志（写失败静默忽略）。"""
        try:
            _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} | stage={stage} =====\n")
                f.write(f"command: {command}\n")
                f.write(f"cwd: {cwd}\n")
                f.write(f"exception: {type(exc).__name__}: {exc!r}\n")
                f.write(traceback.format_exc())
        except Exception:
            pass
