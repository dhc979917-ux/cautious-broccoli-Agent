"""Tier 1/2 驱动：以子进程跑 render_probe.mjs（node + Playwright）。

可用性优先于能力：Playwright/Chromium 不在环境里时，本层直接返回
``available=False`` 与原因，验证退化为 Tier 0 静态检查，绝不抛异常、
绝不阻断交付。这样同一份代码在本地开发机、后端容器、缺浏览器的旧镜像上
都能跑，只是能力档位不同（能力差异会写进结论，traces/监控里看得见）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 探针脚本与本文件同目录（随镜像一起打包，不需要额外挂载）
_PROBE = Path(__file__).resolve().parent / "render_probe.mjs"


def _playwright_candidates() -> list[str]:
    """按优先级列出 playwright 包目录候选；返回的第一个存在者即为所用。"""
    out: list[str] = []
    explicit = (getattr(settings, "ARTIFACT_VERIFY_PLAYWRIGHT_DIR", "") or "").strip()
    if explicit:
        out.append(explicit)
        out.append(str(Path(explicit) / "node_modules" / "playwright"))
    here = Path(__file__).resolve().parent
    out.append(str(here / "node_modules" / "playwright"))
    # 后端根目录（镜像里是 /app）：Dockerfile 会把 playwright 装在这里
    backend_root = here.parents[2]
    out.append(str(backend_root / "node_modules" / "playwright"))
    # 本地开发：仓库根的 test/ 已声明 playwright 依赖
    out.append(str(backend_root.parent / "test" / "node_modules" / "playwright"))
    seen: set[str] = set()
    uniq = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def resolve_playwright() -> Optional[str]:
    """找到可用的 playwright 包目录；找不到返回 None（此时仍可试裸模块名）。"""
    for cand in _playwright_candidates():
        if os.path.isdir(cand):
            return cand
    return None


def node_binary() -> Optional[str]:
    configured = (getattr(settings, "ARTIFACT_VERIFY_NODE", "") or "").strip()
    if configured:
        found = shutil.which(configured) or (configured if os.path.isfile(configured) else None)
        if found:
            return found
    return shutil.which("node") or shutil.which("nodejs")


def is_available() -> tuple[bool, str]:
    """渲染层是否可用 + 不可用原因（写进结论，便于运维一眼看出降级）。"""
    if not getattr(settings, "ARTIFACT_VERIFY_RENDER", True):
        return False, "render_disabled_by_config"
    if not _PROBE.is_file():
        return False, "probe_script_missing"
    if not node_binary():
        return False, "node_not_found"
    return True, ""


async def run(
    path: str,
    *,
    screenshot_path: Optional[str] = None,
    timeout: Optional[int] = None,
    max_clicks: Optional[int] = None,
) -> dict[str, Any]:
    """跑一次无头渲染检查，返回探针的 JSON 结论。

    任何失败都转成 ``{"ok": False, "available": False, "reason": ...}``，
    调用方据此降级，不需要 try/except。
    """
    ok, reason = is_available()
    if not ok:
        return {"ok": False, "available": False, "reason": reason}

    cfg = {
        "path": path,
        "width": 1440,
        "height": 900,
        "settleMs": 1200,
        "navTimeout": 15000,
        # 用 load 而不是 networkidle：产物多为 file:// 单文件页，networkidle 会
        # 一直等到外链请求超时（离线环境下 DNS 失败要好几秒），实测白等 15s+。
        # load 之后另有 settleMs 静置，足够图表完成首帧渲染。
        "waitUntil": "load",
        "maxClicks": max_clicks if max_clicks is not None else getattr(settings, "ARTIFACT_VERIFY_MAX_CLICKS", 4),
        "clickProbe": True,
    }
    pw_dir = resolve_playwright()
    if pw_dir:
        cfg["playwrightEntry"] = pw_dir
    if screenshot_path:
        cfg["screenshotPath"] = screenshot_path

    limit = timeout or getattr(settings, "ARTIFACT_VERIFY_TIMEOUT_SECONDS", 90)

    # 用同步 subprocess + to_thread，不用 asyncio.create_subprocess_exec：
    # 与 core/robust_backend.py 同一套做法。uvicorn --reload 在 Windows 上会把
    # 事件循环策略强制成 SelectorEventLoop，而它不支持 asyncio 子进程
    # （实测抛 PermissionError/NotImplementedError），Tier1 会静默永远跑不起来。
    def _run_sync() -> tuple[bytes, bytes] | str:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(
                [node_binary() or "node", str(_PROBE)],
                input=json.dumps(cfg).encode("utf-8"),
                capture_output=True,
                cwd=str(_PROBE.parent),
                timeout=limit,
                env=env,
            )
            return proc.stdout or b"", proc.stderr or b""
        except subprocess.TimeoutExpired:
            return f"render_timeout({limit}s)"
        except (OSError, ValueError) as e:
            return f"spawn_failed: {e}"

    result = await asyncio.to_thread(_run_sync)
    if isinstance(result, str):
        # 超时算"跑过但没结果"，起进程失败算"渲染层不可用"（后者要提示运维装依赖）
        available = not result.startswith("spawn_failed")
        return {"ok": False, "available": available, "reason": result}
    stdout, stderr = result

    text = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        return {
            "ok": False, "available": True,
            "reason": "probe_no_output: " + (err[-300:] if err else "empty"),
        }
    # 探针只输出一个 JSON 对象；万一 node 打了额外日志，取最后一个 { 开头的行
    parsed: Any = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(parsed, dict):
        return {"ok": False, "available": True, "reason": "probe_bad_json: " + text[-200:]}

    parsed.setdefault("available", True)
    # playwright 没装时探针返回 playwright_unavailable，归一成"能力降级"而非"验证失败"
    if str(parsed.get("reason", "")).startswith("playwright_unavailable"):
        parsed["available"] = False
    return parsed
