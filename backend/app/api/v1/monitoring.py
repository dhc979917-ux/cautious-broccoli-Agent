"""监控与运维 API

权限模型：
- 非 admin：所有查询强制 user_id=本人，只能看自己的数据
- admin：可传任意 user_id 查看指定用户；不传 user_id 则看全局聚合
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.deps import get_current_user, has_perm, require_perm
from app.models.user import User
from app.models.session import Session as SessionModel
from app.services.monitoring_service import MonitoringService

router = APIRouter(prefix="/monitoring", tags=["监控与运维"])


def _resolve_user_id(current_user: User, requested: Optional[int]) -> Optional[int]:
    """无监控管理权限强制查自己；有权限者可查指定用户或全局（None）"""
    if not has_perm(current_user, "monitor_manage"):
        return current_user.id
    return requested


@router.get("/overview")
async def get_overview(
    user_id: Optional[int] = None,
    days: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _resolve_user_id(current_user, user_id)
    days = max(1, min(int(days), 365)) if days else None
    return MonitoringService(db).overview(uid, days=days)


@router.get("/trend")
async def get_trend(
    days: int = 30,
    group_by: str = "model",  # model | agent
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _resolve_user_id(current_user, user_id)
    days = max(1, min(int(days or 30), 365))
    gb = group_by if group_by in ("model", "agent") else "model"
    return MonitoringService(db).trend(uid, days, gb)


@router.get("/days/{date}/conversations")
async def get_day_conversations(
    date: str,
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _resolve_user_id(current_user, user_id)
    return MonitoringService(db).conversations_on_day(uid, date)


@router.get("/conversations/{session_id}/detail")
async def get_conversation_detail(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not has_perm(current_user, "monitor_manage") and sess.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权查看该会话")
    return MonitoringService(db).conversation_detail(session_id)


@router.get("/tasks")
async def get_tasks(
    user_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    uid = _resolve_user_id(current_user, user_id)
    return MonitoringService(db).tasks(uid)


@router.get("/users")
async def get_users_overview(
    current_user: User = Depends(require_perm("monitor_manage")),
    db: Session = Depends(get_db),
):
    """用户总览（需 monitor_manage 权限）：点击任一用户可进入该用户的监控视图"""
    return MonitoringService(db).users_overview()


# ==================== Trace 成本/耗时聚合（harness 可观测） ====================
# 数据源为 traces/ 目录的 OTel span 文件（按天轮转，保留 TRACE_RETENTION_DAYS 天）


@router.get("/trace/tasks")
async def get_trace_tasks(
    days: int = 7,
    limit: int = 100,
    current_user: User = Depends(require_perm("monitor_manage")),
):
    """任务成本列表：每个任务（trace）的 token 消耗、模型轮数、工具耗时"""
    from app.services.trace_stats import TraceStatsService
    days = max(1, min(int(days or 7), 90))
    limit = max(1, min(int(limit or 100), 500))
    return TraceStatsService().tasks(days=days, limit=limit)


@router.get("/trace/tools")
async def get_trace_tools(
    days: int = 7,
    current_user: User = Depends(require_perm("monitor_manage")),
):
    """工具耗时画像：各工具调用次数、平均/最大耗时"""
    from app.services.trace_stats import TraceStatsService
    days = max(1, min(int(days or 7), 90))
    return TraceStatsService().tool_profile(days=days)


@router.get("/trace/summary")
async def get_trace_summary(
    days: int = 7,
    current_user: User = Depends(require_perm("monitor_manage")),
):
    """区间汇总：任务数、模型调用、token 总量、重任务 TOP"""
    from app.services.trace_stats import TraceStatsService
    days = max(1, min(int(days or 7), 90))
    return TraceStatsService().summary(days=days)


# ==================== 记忆生命周期 janitor（运维补跑入口） ====================
# 正常由 main.py 的定时器每日 03:00 驱动（启动后还会延迟补跑一次）。这里给运维
# 一个手动入口：漏跑补跑、改完提炼逻辑立刻验证，不必等第二天凌晨。


@router.post("/memory/janitor")
async def trigger_memory_janitor(
    at: Optional[str] = None,
    wait: bool = False,
    current_user: User = Depends(require_perm("monitor_manage")),
):
    """手动触发一次记忆 janitor：提炼日总结 -> 聚合周/月总结 -> 清理已吸收原文。

    - ``at``：ISO 日期时间，回放"以那个时刻为准"的一次运行（例如
      ``2026-09-05T03:00:00`` 只处理 9/5 之前该处理的内容）；不传按当前时间。
    - ``wait``：true 同步等结果并返回 stats（本地验证用）；false（默认）丢后台
      立即返回，结果查 ``GET /monitoring/memory/janitor/status``。janitor 要串行
      跑完所有用户的 LLM 提炼，同步等待容易被网关超时掐断。

    已有运行在进行时返回 ``skipped``，不会并发跑第二次（并发会重复提炼/删除源卡）。
    """
    from datetime import datetime as _dt
    from app.agents.memory import run_janitor_once

    now = None
    if at:
        try:
            now = _dt.fromisoformat(str(at).strip())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="at 需为 ISO 格式，例如 2026-09-05T03:00:00",
            )
    result = await run_janitor_once(now, wait=bool(wait))
    return {"ok": True, "triggered_by": current_user.username,
            "at": (now or _dt.now()).isoformat(timespec="seconds"), **result}


@router.get("/memory/janitor/status")
async def memory_janitor_status(
    current_user: User = Depends(require_perm("monitor_manage")),
):
    """janitor 运行状态：是否在跑、累计跑过几次、上一次的 stats 与错误。"""
    from app.agents.memory import janitor_status
    return janitor_status()
