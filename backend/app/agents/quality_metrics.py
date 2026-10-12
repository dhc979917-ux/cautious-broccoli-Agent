"""质量闭环指标：采集/验证动作的最小记账，供 traces 与监控页聚合。

为什么单独成模块：写指标的地方有三处（BuildProfileMiddleware 记档位、
QualityMetricsMiddleware 记采集调用、VerifyArtifact 工具记验证结论），
读的地方有两处（manager 的 done 事件、监控聚合）。口径散落必然漂，
所以键名与累加规则集中在这里，其他地方只调 record/snapshot。

存放位置与档位同一处（AgentState.middle_context），随会话缓存存活，
不落 DB、不加表，因此对现有数据模型零影响。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CTX_KEY = "quality_metrics"

# 计数型指标（每次 record 累加）
_COUNTERS = (
    "intake_calls",        # CollectRequirements 调用次数
    "interview_calls",     # CollectRequirements(interview 模式) 调用次数（grill-me 等多轮拷问）
    "intake_questions",    # 累计提问数
    "intake_assumed",      # 累计按 assume 兜底的题数
    "verify_calls",        # VerifyArtifact 调用次数
    "verify_errors",       # 累计报出的 error 数
    "verify_fix_rounds",   # 修复后复验的次数（第 2 次及以后的 verify 调用）
)


def _store(agent_or_state: Any) -> Optional[dict]:
    """兼容传 agent 或传 AgentState：两处调用方手上拿到的对象不一样。"""
    target = getattr(agent_or_state, "state", agent_or_state)
    ctx = getattr(target, "middle_context", None)
    if not isinstance(ctx, dict):
        return None
    store = ctx.get(_CTX_KEY)
    if not isinstance(store, dict):
        store = {}
        ctx[_CTX_KEY] = store
    return store


def record(agent_or_state: Any, **values: Any) -> None:
    """写指标。计数型键累加，其余覆盖；未知键一律忽略（防止调用方写脏数据）。"""
    store = _store(agent_or_state)
    if store is None:
        return
    for k, v in values.items():
        if k in _COUNTERS:
            try:
                store[k] = int(store.get(k, 0)) + int(v or 0)
            except (TypeError, ValueError):
                continue
        else:
            store[k] = v


def snapshot(agent: Any) -> dict[str, Any]:
    """只读快照（档位 + 采集/验证计数），供 done 事件与 trace 属性使用。"""
    from app.agents import delivery_profile as dp

    out: dict[str, Any] = dict(dp.snapshot(agent))
    store = _store(agent) or {}
    for k in _COUNTERS:
        if store.get(k):
            out[k] = int(store[k])
    for k in ("verify_tier", "verify_ok", "verify_layout_risk", "verify_degraded", "last_verified_path"):
        if k in store:
            out[k] = store[k]
    return out
