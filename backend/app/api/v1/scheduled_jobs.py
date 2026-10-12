"""调度任务 API：任务 CRUD、启停、手动触发、执行历史、触发预览与统计。

权限模型（二期）：
- 所有任务统一由"任务助手"Agent 执行（按配置名解析，创建时不可选择）；
- 管理员可新建公共任务（is_public=True，全员可见、可被选择）或个人任务
  （is_public=False，仅自己可见、不可被选择）；普通用户从公共任务选择生成
  个人实例（parent_id 指向公共任务，is_public 强制 False）；
- 可见性：所有人（含管理员）一律=公共任务 + 本人任务/实例，看不到他人的个人实例
  （回收站同理）；执行历史只能看自己的，管理员对自己创建的公共任务可聚合其派生
  实例并按人员过滤，用于公共任务的运行情况巡检；
- 执行策略固化：同日同用户共享会话、超时不重试、错过触发补跑一次、每次都通知；
- 删除统一为软删除（回收站）：公共任务仅管理员可删/恢复/彻底删除（级联实例），
  个人任务与用户实例由属主或管理员操作。
"""
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, has_perm
from app.database import get_db
from app.models.agent import Agent
from app.models.scheduled_job import ScheduledJob, ScheduledJobRun
from app.models.user import User
from app.services.skill_hub.runtime import binding_description
from app.models.user_agent_access import UserAgentAccess
from app.schemas.scheduled_job import (
    ScheduledJobCreate, ScheduledJobUpdate, SchedulePreviewRequest,
)
from app.services.scheduler_service import (
    describe_schedule, preview_fire_times,
    scheduler_service, validate_trigger,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled-jobs", tags=["调度任务"])

# 执行策略创建时固化（产品侧不提供选项）
FIXED_POLICY = dict(
    skip_if_running=False,          # 上一轮未结束时新开一个会话并行
    timeout_minutes=60,             # 保留超时终止机制，超时不重试
    max_retries=0,
    retry_interval_minutes=5,
    misfire_policy="run_once",      # 错过触发时间（服务重启等）恢复后补跑一次
    notify_on="always",             # 结果每次都通知
    notify_channels=["inapp", "email"],
)
# 更新时禁止修改的策略字段
_POLICY_FIELDS = set(FIXED_POLICY.keys())
# 非管理员属主对自己个人实例仅可修改的字段
_INSTANCE_EDITABLE = {"trigger_type", "cron_expr", "run_at", "timezone"}


# ---------------- 工具函数 ----------------

def _is_admin(user: User) -> bool:
    return user.role == "admin"


def _is_public(job: ScheduledJob) -> bool:
    """公共任务判定：以 is_public 列为准；历史空值回退旧的 parent_id 语义"""
    if job.is_public is None:
        return job.parent_id is None
    return bool(job.is_public)


def _resolve_task_agent(db: Session) -> Agent:
    """按配置名解析本模块统一使用的"任务助手"Agent（不硬编码 id）"""
    agent = (db.query(Agent)
             .filter(Agent.name == settings.SCHEDULER_TASK_AGENT_NAME,
                     Agent.is_active == True)  # noqa: E712
             .first())
    if not agent:
        raise HTTPException(
            status_code=400,
            detail=f"未找到可用的“{settings.SCHEDULER_TASK_AGENT_NAME}”Agent，"
                   "请先在 Agent 管理中创建并启用")
    return agent


def _fetch_job(db: Session, job_id: int) -> ScheduledJob:
    job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def _get_active_job(db: Session, job_id: int, user: User,
                    need_write: bool = False) -> ScheduledJob:
    """取未软删任务并做可见/写权限校验；越权按不存在处理"""
    job = _fetch_job(db, job_id)
    if job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if need_write:
        if not (has_perm(user, "schedule_manage.view_all") or job.user_id == user.id):
            raise HTTPException(status_code=404, detail="任务不存在")
    else:
        if not (has_perm(user, "schedule_manage.view_all") or job.user_id == user.id or _is_public(job)):
            raise HTTPException(status_code=404, detail="任务不存在")
    return job


def _job_dict(db: Session, job: ScheduledJob) -> dict:
    owner = db.query(User).filter(User.id == job.user_id).first()
    return {
        "id": job.id, "user_id": job.user_id,
        "owner_name": owner.username if owner else None,
        "parent_id": job.parent_id, "is_public": _is_public(job),
        "deleted_at": job.deleted_at,
        "agent_id": job.agent_id,
        "name": job.name, "description": job.description, "prompt": job.prompt,
        "trigger_type": job.trigger_type, "cron_expr": job.cron_expr,
        "run_at": job.run_at, "timezone": job.timezone,
        "schedule_desc": describe_schedule(job.trigger_type, job.cron_expr, job.run_at),
        "model": job.model, "selected_skills": job.selected_skills,
        "selected_mcps": job.selected_mcps,
        "skill_versions": binding_description(db, job),
        "enabled": job.enabled, "skip_if_running": job.skip_if_running,
        "timeout_minutes": job.timeout_minutes, "max_retries": job.max_retries,
        "retry_interval_minutes": job.retry_interval_minutes,
        "misfire_policy": job.misfire_policy,
        "notify_on": job.notify_on, "notify_channels": job.notify_channels or [],
        "last_run_at": job.last_run_at, "last_status": job.last_status,
        "next_run_at": job.next_run_at, "created_at": job.created_at,
    }


def _owners_map(db: Session, job_ids) -> dict:
    """批量取 {job_id: (user_id, username)}，避免执行历史逐条查属主"""
    ids = list({i for i in job_ids if i is not None})
    if not ids:
        return {}
    rows = (db.query(ScheduledJob.id, ScheduledJob.user_id, User.username)
            .outerjoin(User, User.id == ScheduledJob.user_id)
            .filter(ScheduledJob.id.in_(ids)).all())
    return {jid: (uid, name) for jid, uid, name in rows}


def _run_dict(run: ScheduledJobRun, owners: dict) -> dict:
    """执行记录 -> 响应。owners 由 _owners_map 提供，公共任务的聚合视图里
    用来标明每条记录属于谁（前端据此决定"查看会话"是跳转还是只读预览）。"""
    owner_id, owner_name = owners.get(run.job_id, (None, None))
    return {
        "id": run.id, "job_id": run.job_id, "session_id": run.session_id,
        "owner_id": owner_id, "owner_name": owner_name,
        "trigger_type": run.trigger_type, "attempt": run.attempt,
        "status": run.status, "error": run.error,
        "reply_summary": run.reply_summary,
        "started_at": run.started_at, "finished_at": run.finished_at,
    }


def _validate_trigger_or_400(trigger_type, cron_expr, run_at, timezone):
    try:
        validate_trigger(trigger_type, cron_expr, run_at, timezone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"触发配置不合法: {e}")


async def _hard_delete_jobs(db: Session, job_ids: List[int]):
    """彻底删除任务：注销调度、终止运行中的执行并留痕、删 runs 与任务行"""
    if not job_ids:
        return
    for jid in job_ids:
        scheduler_service.unregister(jid)
    running = (db.query(ScheduledJobRun)
               .filter(ScheduledJobRun.job_id.in_(job_ids),
                       ScheduledJobRun.status == "running").all())
    if running:
        from app.services.task_manager import task_manager
        for r in running:
            if r.session_id:
                asyncio.create_task(task_manager.cancel(r.session_id, timeout=10))
            r.status = "failed"
            r.error = "任务已删除，执行被终止"
            r.finished_at = datetime.now()
    (db.query(ScheduledJobRun)
       .filter(ScheduledJobRun.job_id.in_(job_ids))
       .delete(synchronize_session=False))
    (db.query(ScheduledJob)
       .filter(ScheduledJob.id.in_(job_ids))
       .delete(synchronize_session=False))
    db.commit()


# ---------------- 列表 / 统计 / 预览 ----------------

@router.get("/")
def list_jobs(scope: Optional[str] = None, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """任务列表。可见性口径：

    - 默认：公共任务 + 本人任务/实例，他人的个人实例不暴露；
    - scope=all（仅管理员）：全部未删除任务，含他人的个人任务与实例；
    - scope=deleted：回收站。管理员看全量回收站，普通用户仍只看公共 + 本人，
      与"全部"分组保持同一口径，避免管理员在两个视图里看到的范围不一致。
    """
    if scope == "all" and not has_perm(current_user, "schedule_manage.view_all"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    # 管理员的全量视图：跳过"公共 + 本人"这层可见性过滤
    admin_wide_view = has_perm(current_user, "schedule_manage.view_all") and scope in ("all", "deleted")

    q = db.query(ScheduledJob)
    if scope == "deleted":
        q = q.filter(ScheduledJob.deleted_at != None)  # noqa: E711
    else:
        q = q.filter(ScheduledJob.deleted_at == None)  # noqa: E711
    if not admin_wide_view:
        q = q.filter(or_(ScheduledJob.is_public == True,  # noqa: E712
                         ScheduledJob.user_id == current_user.id))
    jobs = q.order_by(ScheduledJob.id.asc()).all()
    return [_job_dict(db, j) for j in jobs]


@router.get("/stats")
def job_stats(db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """统计卡：与任务列表口径一致，所有人（含管理员）只统计本人名下的任务与执行"""
    job_q = (db.query(ScheduledJob)
             .filter(ScheduledJob.deleted_at == None,  # noqa: E711
                     ScheduledJob.user_id == current_user.id))
    run_q = (db.query(ScheduledJobRun)
             .join(ScheduledJob, ScheduledJobRun.job_id == ScheduledJob.id)
             .filter(ScheduledJob.deleted_at == None,  # noqa: E711
                     ScheduledJob.user_id == current_user.id))

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_total = run_q.filter(ScheduledJobRun.started_at >= today_start).count()
    today_success = run_q.filter(ScheduledJobRun.started_at >= today_start,
                                 ScheduledJobRun.status == "success").count()
    next_job = (job_q
                .filter(ScheduledJob.enabled == True,  # noqa: E712
                        ScheduledJob.next_run_at != None)  # noqa: E711
                .order_by(ScheduledJob.next_run_at.asc()).first())
    return {
        "enabled": job_q.filter(ScheduledJob.enabled == True).count(),  # noqa: E712
        "running": run_q.filter(ScheduledJobRun.status == "running").count(),
        "today_total": today_total,
        "today_success": today_success,
        "next": ({"job_id": next_job.id, "name": next_job.name,
                  "at": next_job.next_run_at} if next_job else None),
    }


@router.post("/preview")
def preview_schedule(data: SchedulePreviewRequest,
                     current_user: User = Depends(get_current_user)):
    """触发配置预览：中文描述 + 未来 3 次触发时间（不落库）"""
    _validate_trigger_or_400(data.trigger_type, data.cron_expr, data.run_at, data.timezone)
    times = preview_fire_times(data.trigger_type, data.cron_expr, data.run_at,
                               data.timezone, count=3)
    return {
        "description": describe_schedule(data.trigger_type, data.cron_expr, data.run_at),
        "next_runs": [t.strftime("%Y-%m-%d %H:%M") for t in times],
    }


# ---------------- CRUD ----------------

@router.post("/")
def create_job(data: ScheduledJobCreate, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """新建任务：管理员建公共任务或个人任务（is_public）；
    任何用户传 parent_id 选择公共任务生成个人实例（is_public 强制 False）"""
    _validate_trigger_or_400(data.trigger_type, data.cron_expr, data.run_at, data.timezone)
    if data.trigger_type == "once" and data.run_at <= datetime.now():
        raise HTTPException(status_code=400, detail="一次性任务的执行时间必须是未来时间")

    if data.parent_id:
        # 选择公共任务 → 复制内容为个人实例（快照，父任务后续编辑不回溯）
        parent = _fetch_job(db, data.parent_id)
        if (parent.parent_id is not None or not _is_public(parent)
                or parent.deleted_at is not None):
            raise HTTPException(status_code=404, detail="所选任务不存在或已删除")
        agent = db.query(Agent).filter(Agent.id == parent.agent_id).first()
        if not agent or not agent.is_active:
            raise HTTPException(status_code=400, detail="任务绑定的 Agent 不存在或已停用")
        job = ScheduledJob(
            user_id=current_user.id, agent_id=parent.agent_id, parent_id=parent.id,
            is_public=False,
            name=parent.name, description=parent.description, prompt=parent.prompt,
            model=parent.model, selected_skills=parent.selected_skills,
            selected_mcps=parent.selected_mcps,
            trigger_type=data.trigger_type, cron_expr=data.cron_expr,
            run_at=data.run_at, timezone=data.timezone,
            **FIXED_POLICY,
        )
        # 执行链路会复查 Agent 访问权限：缺失时自动授予（本模块统一走任务助手）
        if not has_perm(current_user, "schedule_manage.create"):
            exists = (db.query(UserAgentAccess)
                      .filter(UserAgentAccess.user_id == current_user.id,
                              UserAgentAccess.agent_id == parent.agent_id).first())
            if exists and exists.access_level not in ("execute", "admin"):
                raise HTTPException(status_code=403, detail="当前账号只有 Agent 查看权限")
            if not exists:
                db.add(UserAgentAccess(user_id=current_user.id,
                                       agent_id=parent.agent_id,
                                       access_level="execute"))
    else:
        if not has_perm(current_user, "schedule_manage.create"):
            raise HTTPException(status_code=403, detail="无新建调度任务权限")
        # 公共任务：名称与内容必填（个人实例从父任务复制，不经过此分支）
        if not data.name or not data.name.strip():
            raise HTTPException(status_code=400, detail="请填写任务名称")
        if not data.prompt or not data.prompt.strip():
            raise HTTPException(status_code=400, detail="请填写任务内容")
        agent = _resolve_task_agent(db)
        job = ScheduledJob(
            user_id=current_user.id, agent_id=agent.id, parent_id=None,
            is_public=bool(data.is_public),
            name=data.name.strip(),
            description=(data.description or "").strip() or None,
            prompt=data.prompt.strip(),
            trigger_type=data.trigger_type, cron_expr=data.cron_expr,
            run_at=data.run_at, timezone=data.timezone,
            model=data.model, selected_skills=data.selected_skills,
            selected_mcps=data.selected_mcps,
            **FIXED_POLICY,
        )

    db.add(job)
    db.flush()
    from app.services.skill_hub.runtime import bind_job
    bind_job(db, job, current_user)
    db.commit()
    db.refresh(job)
    scheduler_service.register(job)  # 回填 next_run_at 并注册调度
    db.commit()
    kind = ("个人实例" if job.parent_id
            else ("公共任务" if _is_public(job) else "个人任务"))
    logger.info(f"[scheduled-jobs] 用户 {current_user.id} 创建任务 {job.id}: "
                f"{job.name}（{kind}）")
    return _job_dict(db, job)


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    return _job_dict(db, _get_active_job(db, job_id, current_user))


@router.put("/{job_id}")
def update_job(job_id: int, data: ScheduledJobUpdate, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    job = _get_active_job(db, job_id, current_user, need_write=True)
    patch = data.model_dump(exclude_unset=True)
    # Agent 与执行策略固化，忽略修改
    patch.pop("agent_id", None)
    for k in _POLICY_FIELDS:
        patch.pop(k, None)
    if not has_perm(current_user, "schedule_manage.update"):
        if _is_public(job):
            raise HTTPException(status_code=403, detail="无编辑调度任务权限")
        # 个人实例仅允许改触发配置（执行时间）
        patch = {k: v for k, v in patch.items() if k in _INSTANCE_EDITABLE}

    # 触发配置合并校验（部分更新时与现有字段组合判断）
    merged_trigger = patch.get("trigger_type", job.trigger_type)
    merged_cron = patch.get("cron_expr", job.cron_expr)
    merged_run_at = patch.get("run_at", job.run_at)
    merged_tz = patch.get("timezone", job.timezone)
    _validate_trigger_or_400(merged_trigger, merged_cron, merged_run_at, merged_tz)
    if merged_trigger == "once" and merged_run_at and merged_run_at <= datetime.now() \
            and patch.get("enabled", job.enabled):
        raise HTTPException(status_code=400, detail="一次性任务的执行时间必须是未来时间")

    update_skill_binding = "selected_skills" in patch
    for k, v in patch.items():
        setattr(job, k, v)
    if update_skill_binding:
        from app.services.skill_hub.runtime import bind_job
        bind_job(db, job, db.get(User, job.user_id))
    scheduler_service.register(job)
    db.commit()
    return _job_dict(db, job)


@router.patch("/{job_id}/toggle")
def toggle_job(job_id: int, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    job = _get_active_job(db, job_id, current_user, need_write=True)
    if not (has_perm(current_user, "schedule_manage.toggle") or job.user_id == current_user.id):
        raise HTTPException(status_code=403, detail="无启用/停用调度任务权限")
    if not job.enabled and job.trigger_type == "once" \
            and (not job.run_at or job.run_at <= datetime.now()):
        raise HTTPException(status_code=400, detail="一次性任务已过执行时间，无法再启用")
    job.enabled = not job.enabled
    scheduler_service.register(job)
    db.commit()
    return _job_dict(db, job)


@router.delete("/{job_id}")
async def delete_job(job_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """统一软删除（移入回收站，可恢复）：
    公共任务仅管理员；个人任务/用户实例由属主或管理员操作。"""
    job = _get_active_job(db, job_id, current_user, need_write=True)
    public = _is_public(job)
    if public and not has_perm(current_user, "schedule_manage.delete.soft"):
        raise HTTPException(status_code=403, detail="无删除调度任务权限")
    job.deleted_at = datetime.now()
    scheduler_service.unregister(job.id)
    job.next_run_at = None
    db.commit()
    logger.info(f"[scheduled-jobs] 用户 {current_user.id} 软删除任务 {job_id}"
                f"（{'公共任务' if public else '个人任务/实例'}）")
    return {"message": "任务已移入回收站"}


@router.delete("/{job_id}/hard")
async def hard_delete_job(job_id: int, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """彻底删除：公共任务仅管理员（级联其全部个人实例）；
    个人任务/用户实例由属主或管理员操作；可在回收站执行。"""
    job = _fetch_job(db, job_id)
    public = _is_public(job)
    if public:
        if not has_perm(current_user, "schedule_manage.delete.hard"):
            raise HTTPException(status_code=403, detail="无彻底删除调度任务权限")
    elif not (has_perm(current_user, "schedule_manage.delete.hard") or job.user_id == current_user.id):
        raise HTTPException(status_code=404, detail="任务不存在")
    ids = [job.id]
    if public:
        ids += [c.id for c in db.query(ScheduledJob)
                .filter(ScheduledJob.parent_id == job.id).all()]
    await _hard_delete_jobs(db, ids)
    logger.info(f"[scheduled-jobs] 用户 {current_user.id} 彻底删除任务 {job_id}"
                f"（含关联 {len(ids) - 1} 个实例）")
    return {"message": "任务已彻底删除"}


@router.post("/{job_id}/restore")
def restore_job(job_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """从回收站恢复：公共任务仅管理员；个人任务/用户实例由属主或管理员操作"""
    job = _fetch_job(db, job_id)
    if _is_public(job):
        if not has_perm(current_user, "schedule_manage.delete.restore"):
            raise HTTPException(status_code=403, detail="无恢复调度任务权限")
    elif not (has_perm(current_user, "schedule_manage.delete.restore") or job.user_id == current_user.id):
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.deleted_at is None:
        raise HTTPException(status_code=400, detail="任务未被删除")
    job.deleted_at = None
    scheduler_service.register(job)
    db.commit()
    logger.info(f"[scheduled-jobs] 用户 {current_user.id} 恢复任务 {job_id}")
    return _job_dict(db, job)


# ---------------- 手动触发 / 执行历史 ----------------

@router.post("/{job_id}/run")
async def run_job_now(job_id: int, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    job = _get_active_job(db, job_id, current_user, need_write=True)
    # 立即执行不受 enabled 限制（手动语义），校验与执行由 fire 链路负责
    asyncio.create_task(scheduler_service.fire(job.id, "manual", 1, force=True))
    logger.info(f"[scheduled-jobs] 用户 {current_user.id} 手动触发任务 {job_id}")
    return {"message": "已触发手动执行，结果见执行历史"}


@router.get("/{job_id}/runs")
def list_runs(job_id: int, limit: int = Query(20, ge=1, le=100), offset: int = 0,
              user_id: Optional[int] = Query(
                  None, description="仅管理员+公共任务生效：按实例属主过滤执行历史"),
              db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """执行历史：只能看自己的。

    - 个人任务 / 用户实例：仅属主可查（管理员不再有旁路）
    - 公共任务：仅创建者可查；管理员额外可聚合该公共任务派生的全部个人实例，
      并用 user_id 按人员过滤，每条记录带 owner_id/owner_name 标明归属
    """
    job = _fetch_job(db, job_id)
    # 越权与已删除统一按"任务不存在"处理，不泄漏他人任务是否存在
    if job.deleted_at is not None or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="任务不存在")

    scope_q = db.query(ScheduledJob.id).filter(ScheduledJob.id == job.id)
    if _is_public(job) and has_perm(current_user, "schedule_manage.run"):
        scope_q = (db.query(ScheduledJob.id)
                   .filter(ScheduledJob.deleted_at == None,  # noqa: E711
                           or_(ScheduledJob.id == job.id,
                               ScheduledJob.parent_id == job.id)))
        if user_id is not None:
            scope_q = scope_q.filter(ScheduledJob.user_id == user_id)
    job_ids = [i for (i,) in scope_q.all()]

    q = (db.query(ScheduledJobRun)
         .filter(ScheduledJobRun.job_id.in_(job_ids))
         .order_by(ScheduledJobRun.id.desc()))
    total = q.count()
    items = q.offset(offset).limit(limit).all()
    owners = _owners_map(db, [r.job_id for r in items])
    return {"total": total, "items": [_run_dict(r, owners) for r in items]}
