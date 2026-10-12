"""当前活跃回复活动登记表（D7：事件循环 lag 归因）。

事件循环阻塞监控（main.py）只能测出"循环被卡了多久"，答不出"是谁在卡"。
Agent 链路在关键阶段（构建 / 模型调用 / 工具执行）按 session_id 登记当前阶段，
lag 告警与健康探针即可列出活跃回合，直接定位阻塞来源。

实现约束：仅 dict 读写、无锁（只由事件循环线程调用）、零外部依赖，
确保登记动作本身不可能成为新的阻塞点。
"""

import time
from typing import Dict, List, Tuple

# session_id -> (阶段标签, 登记时刻 monotonic)
_active: Dict[str, Tuple[str, float]] = {}

# 最近一次事件循环 lag 测量：lag 监控写入，独立线程健康探针读取
_last_lag: Dict[str, float] = {"lag": 0.0, "ts": 0.0}


def set_active(session_id, label: str) -> None:
    """登记会话当前所处阶段（如 build / react:model#3 / tool:Bash）。"""
    if session_id is None:
        return
    _active[str(session_id)] = (label, time.monotonic())


def clear_active(session_id) -> None:
    if session_id is None:
        return
    _active.pop(str(session_id), None)


def record_lag(lag: float) -> None:
    _last_lag["lag"] = float(lag)
    _last_lag["ts"] = time.time()


def last_lag() -> float:
    return _last_lag["lag"]


def snapshot() -> List[str]:
    """活跃回合快照（供日志与健康探针），形如 ["session=181 tool:Bash (37s)"]。"""
    now = time.monotonic()
    return [
        f"session={sid} {label} ({now - since:.0f}s)"
        for sid, (label, since) in list(_active.items())
    ]
