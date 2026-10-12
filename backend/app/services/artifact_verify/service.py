"""验证编排：Tier 0 必跑 → Tier 1 按需 → Tier 2 限一次，并输出紧凑结论。

两条硬约束（改造的初衷就是省 token，这里必须守住）：
1. 返回给模型的文本有长度上限（settings.ARTIFACT_VERIFY_RESULT_MAX_CHARS），
   超出的明细被折叠成计数。验证结论本身不能变成上下文负担。
2. 任何一层不可用都只是"降级"，不是"失败"：结论里带 degraded 原因，
   模型据此在回复中说明验证覆盖到哪一层，绝不因为缺浏览器就阻断交付。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.services.artifact_verify import render_check, static_check

logger = logging.getLogger(__name__)

# 可验证的产物扩展名：只对文本类前端产物做验证，二进制产物直接拒（省得白跑）
_SUPPORTED_EXT = {".html", ".htm", ".xhtml", ".svg", ".mhtml"}


@dataclass
class VerifyVerdict:
    """一次验证的结构化结论。"""

    ok: bool = False
    tier: int = 0                     # 实际跑到的最高层：0 静态 / 1 渲染 / 2 含截图
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    externals: list[str] = field(default_factory=list)
    layout_risk: bool = False
    screenshot: Optional[str] = None
    degraded: list[str] = field(default_factory=list)   # 哪一层没跑成，为什么
    stats: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tier": self.tier,
            "errors": self.errors,
            "warnings": self.warnings,
            "externals": self.externals,
            "layout_risk": self.layout_risk,
            "screenshot": self.screenshot,
            "degraded": self.degraded,
            "stats": self.stats,
            "elapsed_ms": self.elapsed_ms,
        }


def supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _SUPPORTED_EXT


def format_text(v: VerifyVerdict, path_label: str) -> str:
    """把结论压成模型可直接据以行动的紧凑文本（有长度上限）。"""
    limit = int(getattr(settings, "ARTIFACT_VERIFY_RESULT_MAX_CHARS", 2000) or 2000)
    lines: list[str] = []
    head = f"验证结论：{'通过' if v.ok else '未通过'}（Tier {v.tier}，{v.elapsed_ms}ms）{path_label}"
    lines.append(head)

    if v.degraded:
        lines.append("降级：" + "；".join(v.degraded))
    if v.errors:
        lines.append(f"error（必须修，共 {len(v.errors)} 条）：")
        lines.extend(f"- {e}" for e in v.errors)
    if v.externals:
        lines.append(f"externals（外链资源，离线/内网会失败，共 {len(v.externals)} 条）：")
        lines.extend(f"- {e}" for e in v.externals)
    if v.warnings:
        lines.append(f"warning（择要修，共 {len(v.warnings)} 条）：")
        lines.extend(f"- {w}" for w in v.warnings)

    s = v.stats or {}
    stat_bits = []
    for key, label in (
        ("text_len", "可见文字"),
        ("dom_nodes", "DOM节点"),
        ("canvas_total", "画布"),
        ("interactive_elements", "可交互元素"),
        ("click_probed", "试点击"),
    ):
        if key in s:
            stat_bits.append(f"{label}{s[key]}")
    if stat_bits:
        lines.append("渲染概况：" + "，".join(stat_bits))
    if v.layout_risk:
        lines.append("layout_risk=true：存在溢出或塌陷容器，建议做一次视觉抽查（整轮最多 1 次）")
    if v.ok and not v.warnings:
        lines.append("可直接交付。")
    elif not v.ok:
        lines.append("请用 Edit 定点修复 error 后复验；最多 2 轮，禁止整文件重写。")

    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 60].rstrip() + f"\n…（结论已截断到 {limit} 字符，优先修 error）"
    return text


async def verify(
    path: str,
    *,
    want_screenshot: bool = False,
    render: Optional[bool] = None,
) -> VerifyVerdict:
    """执行分层验证。``path`` 必须是后端进程可直接读取的路径。"""
    started = time.monotonic()
    v = VerifyVerdict()

    if not supported(path):
        v.errors.append(
            f"不支持验证该类型产物（{os.path.splitext(path)[1] or '无扩展名'}）；"
            f"当前只支持 {', '.join(sorted(_SUPPORTED_EXT))}"
        )
        v.degraded.append("unsupported_type")
        v.elapsed_ms = int((time.monotonic() - started) * 1000)
        return v

    # ---- Tier 0：静态检查（同步 + 可能起 node 子进程，丢线程池别堵事件循环）----
    try:
        t0 = await asyncio.to_thread(static_check.run, path)
    except Exception as e:  # 静态层自身出错也不能让验证整体失败
        logger.warning("[verify] Tier0 异常: %s", e)
        t0 = {"ok": False, "errors": [], "warnings": [f"静态检查异常: {e}"], "externals": [], "meta": {}}
    v.errors.extend(t0.get("errors") or [])
    # 外链资源已经单列成 externals 段，不要再混进 warnings 重复占 token
    v.warnings.extend(
        w for w in (t0.get("warnings") or []) if not str(w).startswith("外链资源")
    )
    v.externals.extend(t0.get("externals") or [])
    v.stats.update({k: val for k, val in (t0.get("meta") or {}).items() if k in ("bytes", "id_count", "script_blocks")})
    v.tier = 0

    # ---- Tier 1：无头渲染 ----
    do_render = getattr(settings, "ARTIFACT_VERIFY_RENDER", True) if render is None else render
    if not do_render:
        v.degraded.append("render_disabled_by_config（只做了静态检查）")
    elif not getattr(settings, "ARTIFACT_VERIFY_ENABLED", True):
        v.degraded.append("verify_disabled_by_config")
    else:
        shot_path = None
        if want_screenshot and getattr(settings, "ARTIFACT_VERIFY_SCREENSHOT", True):
            fd, shot_path = tempfile.mkstemp(prefix="verify_shot_", suffix=".png")
            os.close(fd)
        t1 = await render_check.run(path, screenshot_path=shot_path)
        # 两类没跑成：能力不可用（缺 node/Playwright）与运行失败（浏览器没装、
        # 渲染超时、探针崩溃）。都算降级，不能当成"渲染通过、零问题"。
        if not t1.get("available", False) or t1.get("reason"):
            reason = str(t1.get("reason") or "render_unavailable")
            v.degraded.append(f"渲染层不可用：{reason}（只做了静态检查）")
            if shot_path:
                _silent_unlink(shot_path)
        else:
            v.tier = 2 if t1.get("screenshot") else 1
            for key in ("page_errors", "console_errors"):
                for item in t1.get(key) or []:
                    v.errors.append(f"{'页面异常' if key == 'page_errors' else '控制台错误'}: {item}")
            # Tier0 已把外链列进 externals，同一个 URL 不要再以"加载失败"重复报一次
            _known_urls = {u.split("=", 1)[-1] for u in v.externals}
            for item in t1.get("request_failed") or []:
                if any(str(item).endswith(u) or u in str(item) for u in _known_urls):
                    continue
                v.warnings.append(f"资源加载失败: {item}")
            for item in t1.get("console_warnings") or []:
                v.warnings.append(f"控制台警告: {item}")
            for item in t1.get("zero_size_ids") or []:
                v.errors.append(f"容器尺寸塌陷（内容不可见）: {item}")
            for item in t1.get("overflow") or []:
                v.warnings.append(f"横向溢出: {item}")
            if int(t1.get("click_new_errors") or 0) > 0:
                v.errors.append(f"试点击 {t1.get('click_probed')} 个控件后新增 {t1['click_new_errors']} 条报错")
            # 白屏判定要三个条件同时成立：无文字、无画布、DOM 极少。
            # 只看文字长度会把内容简短的合法页面误判成白屏，逼模型无谓返工。
            if (
                int(t1.get("text_len") or 0) == 0
                and int(t1.get("canvas_total") or 0) == 0
                and int(t1.get("dom_nodes") or 0) < 6
            ):
                v.errors.append("页面渲染后完全空白（无文字、无画布、DOM 极少），疑似白屏")
            v.layout_risk = bool(t1.get("layout_risk"))
            v.stats.update({
                k: t1.get(k) for k in (
                    "text_len", "dom_nodes", "canvas_total",
                    "interactive_elements", "click_probed",
                ) if t1.get(k) is not None
            })
            if t1.get("screenshot"):
                v.screenshot = str(t1["screenshot"])
            elif shot_path:
                _silent_unlink(shot_path)

    # 去重并限量，避免同一问题在两层里各报一次
    v.errors = _dedup(v.errors, 14)
    v.warnings = _dedup(v.warnings, 12)
    v.externals = _dedup(v.externals, 8)
    v.ok = not v.errors
    v.elapsed_ms = int((time.monotonic() - started) * 1000)
    return v


def _dedup(items: list[str], limit: int) -> list[str]:
    uniq = list(dict.fromkeys(i for i in items if i))
    if len(uniq) <= limit:
        return uniq
    return uniq[:limit] + [f"…另有 {len(uniq) - limit} 条同类问题（已省略）"]


def _silent_unlink(p: Optional[str]) -> None:
    if not p:
        return
    try:
        os.unlink(p)
    except OSError:
        pass
