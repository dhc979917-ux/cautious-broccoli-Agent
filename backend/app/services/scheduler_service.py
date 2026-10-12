"""调度任务服务：APScheduler 引擎 + 任务执行链路 + 结果通知（模块自包含）。

设计要点（低耦合）：
- 本模块的 3 张表由 ensure_tables() 自建（create_all 幂等），不改 database.py；
- Agent 回合复用聊天链路的后台任务入口（app.api.v1.chat._start_agent_task，
  延迟导入），执行与用户是否在线无关（TaskManager 托管）；
- 通知复用系统邮件（app.core.mailer），站内通知写 notifications 表；
- APScheduler 用内存 JobStore：scheduled_jobs 表是唯一事实源，启动时重建注册。
"""
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import List, Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models.notification import Notification
from app.models.scheduled_job import ScheduledJob, ScheduledJobRun
from app.services.cron_utils import WEEKDAY_ZH, build_cron_trigger, describe_cron

logger = logging.getLogger(__name__)

_STATUS_ZH = {"success": "成功", "failed": "失败", "timeout": "超时终止",
              "skipped": "已跳过", "running": "运行中"}
_TRIGGER_ZH = {"cron": "定时", "once": "一次性", "manual": "手动", "retry": "重试"}
# 迟到判定阈值：实际触发比预期晚超过该秒数才视为"迟到"（触发抖动不算）
_LATE_TOLERANCE_SECONDS = 90


# ---------------- 建表（幂等，仅涉及本模块 3 张表） ----------------

def ensure_tables():
    from app.models import notification, scheduled_job  # noqa: F401 注册模型到 Base
    Base.metadata.create_all(bind=engine)
    # 幂等补列：create_all 不会给已存在的表加新列
    # （v2：parent_id/deleted_at；v3：is_public）
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    pending = [
        ("scheduled_jobs", "parent_id", "INTEGER"),
        ("scheduled_jobs", "deleted_at", "TIMESTAMP"),
        ("scheduled_jobs", "is_public", "BOOLEAN DEFAULT TRUE"),
    ]
    with engine.connect() as conn:
        for table, col, coltype in pending:
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                logger.info(f"[scheduler] 表 {table} 补列 {col}")
        # v3 回填（幂等）：用户实例一律非公共；父任务为空的行保持默认 TRUE
        if insp.has_table("scheduled_jobs"):
            res = conn.execute(text(
                "UPDATE scheduled_jobs SET is_public = FALSE "
                "WHERE parent_id IS NOT NULL AND is_public"))
            if (res.rowcount or 0) > 0:
                logger.info(f"[scheduler] scheduled_jobs 回填 is_public，"
                            f"影响 {res.rowcount} 行")
        conn.commit()


# ---------------- 时间/表达式工具 ----------------

def job_key(job_id: int) -> str:
    return f"scheduled_job_{job_id}"


def _tzinfo(tz: str):
    from zoneinfo import ZoneInfo  # Python 3.9+ 标准库（与 APScheduler 3.11 同源）
    return ZoneInfo(tz)


def _localize(naive_dt: datetime, tz: str):
    """naive 本地时间 -> 任务时区的 aware 时间"""
    return naive_dt.replace(tzinfo=_tzinfo(tz))


def _to_naive_local(aware_dt: datetime) -> datetime:
    """aware 时间 -> 服务器本地 naive 时间（与全库"统一北京时间"约定一致）"""
    return aware_dt.astimezone().replace(tzinfo=None)


def validate_trigger(trigger_type: str, cron_expr: Optional[str],
                     run_at: Optional[datetime], tz: str):
    """校验触发表达式与时区，非法时抛 ValueError（由 API 层转 400）"""
    if trigger_type == "cron":
        if not cron_expr:
            raise ValueError("cron 任务必须提供 cron_expr")
        build_cron_trigger(cron_expr.strip(), tz)   # 非法表达式/时区在此抛 ValueError
    elif trigger_type == "once":
        if not run_at:
            raise ValueError("一次性任务必须提供执行时间 run_at")
        _tzinfo(tz)
    else:
        raise ValueError(f"未知触发类型: {trigger_type}")


def describe_schedule(trigger_type: str, cron_expr: Optional[str],
                      run_at: Optional[datetime]) -> str:
    """执行计划的一句话描述（cron 走 cron_utils.describe_cron 的中文人性化文案）"""
    if trigger_type == "once":
        return f"仅执行一次：{run_at.strftime('%Y-%m-%d %H:%M')}" if run_at else "仅执行一次"
    return describe_cron(cron_expr or "")


def render_prompt(prompt: str, now: Optional[datetime] = None) -> str:
    """内置变量替换：{{date}} {{time}} {{datetime}} {{yesterday}} {{weekday}}"""
    now = now or datetime.now()
    return (prompt
            .replace("{{datetime}}", now.strftime("%Y-%m-%d %H:%M"))
            .replace("{{date}}", now.strftime("%Y-%m-%d"))
            .replace("{{time}}", now.strftime("%H:%M"))
            .replace("{{yesterday}}", (now - timedelta(days=1)).strftime("%Y-%m-%d"))
            .replace("{{weekday}}", WEEKDAY_ZH[now.weekday()]))


def compute_next_run(trigger_type: str, cron_expr: Optional[str],
                     run_at: Optional[datetime], tz: str,
                     after: Optional[datetime] = None) -> Optional[datetime]:
    """下一次触发时间（返回 naive 本地时间；无后续触发返回 None）"""
    try:
        after = after or datetime.now()
        if trigger_type == "once":
            return run_at if run_at and run_at > after else None
        trig = build_cron_trigger((cron_expr or "").strip(), tz)
        base = after if after.tzinfo else _localize(after, tz)
        nxt = trig.get_next_fire_time(None, base)
        return _to_naive_local(nxt) if nxt else None
    except Exception:
        return None


def preview_fire_times(trigger_type: str, cron_expr: Optional[str],
                       run_at: Optional[datetime], tz: str, count: int = 3) -> List[datetime]:
    """未来 count 次触发时间（aware -> naive 本地），供表单预览"""
    if trigger_type == "once":
        return [run_at] if run_at and run_at > datetime.now() else []
    try:
        trig = build_cron_trigger((cron_expr or "").strip(), tz)
    except Exception:
        return []
    out: List[datetime] = []
    base = _localize(datetime.now(), tz)
    prev = None
    for _ in range(max(1, min(count, 10))):
        nxt = trig.get_next_fire_time(prev, base if prev is None else prev)
        if not nxt:
            break
        out.append(_to_naive_local(nxt))
        prev = nxt
    return out


# ---------------- 调度器服务 ----------------

class SchedulerService:
    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        # 按 (用户, 日期) 的会话锁：同日多任务并发触发时避免建出重复会话。
        # _prepare 是同步函数、在事件循环线程内执行，短临界区用 threading.Lock 足够。
        self._session_locks: dict = {}
        self._locks_guard = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self.scheduler and self.scheduler.running)

    # ---------- 生命周期 ----------

    async def start(self):
        """建表 + 启动调度器 + 从数据库重建任务注册（幂等）"""
        ensure_tables()
        if self.running:
            return
        self.scheduler = AsyncIOScheduler(timezone=settings.SCHEDULER_TIMEZONE)
        self.scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self.scheduler.start()
        db = SessionLocal()
        count = 0
        try:
            jobs = (db.query(ScheduledJob)
                    .filter(ScheduledJob.enabled == True,  # noqa: E712
                            ScheduledJob.deleted_at == None)  # noqa: E711
                    .all())
            now = datetime.now()
            for j in jobs:
                try:
                    # 过期的一次性任务启动时直接失效
                    if j.trigger_type == "once" and (not j.run_at or j.run_at <= now):
                        j.enabled = False
                        j.next_run_at = None
                        continue
                    self._add_ap_job(j)
                    j.next_run_at = compute_next_run(
                        j.trigger_type, j.cron_expr, j.run_at, j.timezone)
                    count += 1
                except Exception as e:
                    logger.warning(f"[scheduler] 任务 {j.id} 注册失败: {e}")
            db.commit()
        finally:
            db.close()
        logger.info(f"[OK] 调度任务引擎已启动（APScheduler，注册 {count} 个任务）")

    async def shutdown(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            logger.info("[OK] 调度任务引擎已停止")

    # ---------- 注册管理（API 层 CRUD 后调用） ----------

    def _build_trigger(self, job_row: ScheduledJob):
        if job_row.trigger_type == "once":
            return DateTrigger(run_date=job_row.run_at, timezone=job_row.timezone)
        # cron_expr 按标准 crontab 语义存储，星期字段的转换收口在 cron_utils
        return build_cron_trigger(job_row.cron_expr.strip(), job_row.timezone)

    def _add_ap_job(self, job_row: ScheduledJob):
        if not self.running:
            return
        self.scheduler.add_job(
            _ap_fire, self._build_trigger(job_row),
            args=[job_row.id, job_row.trigger_type, 1],
            id=job_key(job_row.id), replace_existing=True,
            misfire_grace_time=settings.SCHEDULER_MISFIRE_GRACE_SECONDS,
            coalesce=True, max_instances=1,
        )

    def _remove_ap_job(self, job_id: int):
        if not self.running:
            return
        try:
            self.scheduler.remove_job(job_key(job_id))
        except Exception:
            pass

    def register(self, job_row: ScheduledJob):
        """新增/更新任务后重建注册并回填 next_run_at（调用方负责 commit）"""
        self._remove_ap_job(job_row.id)
        if job_row.enabled:
            self._add_ap_job(job_row)
        job_row.next_run_at = compute_next_run(
            job_row.trigger_type, job_row.cron_expr, job_row.run_at, job_row.timezone)

    def unregister(self, job_id: int):
        self._remove_ap_job(job_id)

    # ---------- 当日共享会话 ----------

    def _day_session_lock(self, user_id: int, day: str) -> threading.Lock:
        """懒创建 (用户, 日期) 维度的会话锁"""
        key = f"{user_id}:{day}"
        with self._locks_guard:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    def _resolve_day_session(self, db, session_service, user, agent,
                             job_name: str, now: datetime):
        """同一用户同一天的所有任务共用一个会话（消息追加），会话名为 YYYY-MM-DD，
        次日自动新开；当日会话正忙时回退独立会话，保留"上一轮未结束并行"语义。"""
        from app.models.session import Session as ChatSessionModel  # 区别于 ORM Session
        from app.services.task_manager import task_manager

        day_title = now.strftime("%Y-%m-%d")
        with self._day_session_lock(user.id, day_title):
            existing = (db.query(ChatSessionModel)
                        .filter(ChatSessionModel.user_id == user.id,
                                ChatSessionModel.agent_id == agent.id,
                                ChatSessionModel.title == day_title)
                        .first())
            if existing and task_manager.is_running(existing.id):
                logger.info(f"[scheduler] 当日会话 {existing.id} 正忙，"
                            f"任务「{job_name}」回退独立会话")
                return session_service.create_session(
                    user_id=user.id, agent_id=agent.id,
                    title=f"{day_title} {job_name}")
            if existing:
                return existing
            return session_service.create_session(
                user_id=user.id, agent_id=agent.id, title=day_title)

    # ---------- APScheduler 事件 ----------

    def _on_job_missed(self, event):
        """错过触发窗口（服务未运行/超载，超出 misfire 宽限）：补一条 skipped 记录便于审计"""
        try:
            parts = str(event.job_id).split("_")
            if len(parts) < 3 or parts[0] != "scheduled" or parts[1] != "job":
                return
            job_id = int(parts[2])
        except (ValueError, IndexError):
            return
        db = SessionLocal()
        try:
            if db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first():
                db.add(ScheduledJobRun(
                    job_id=job_id, trigger_type="cron", attempt=1, status="skipped",
                    error="错过触发窗口（服务未运行或负载过高），本次未执行"))
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _on_job_error(self, event):
        logger.error(f"[scheduler] 任务触发异常: job_id={event.job_id} "
                     f"exception={event.exception}")

    # ---------- 执行链路 ----------

    async def fire(self, job_id: int, trigger_type: str, attempt: int = 1,
                   force: bool = False) -> Optional[int]:
        """执行一次任务，返回 run 记录 id；任务无效/被跳过时也可能产生记录"""
        ctx = self._prepare(job_id, trigger_type, attempt, force)
        if ctx is None:
            return None
        try:
            status, error, summary = await self._execute(ctx)
        except Exception as e:
            logger.exception(f"[scheduler] 任务 {job_id} 执行异常")
            status, error, summary = "failed", f"执行异常: {e}", ""
        await self._finalize(ctx, status, error, summary)
        return ctx["run_id"]

    def _prepare(self, job_id: int, trigger_type: str, attempt: int, force: bool):
        """前置校验 + 建会话 + 落 user 消息 + 建 run 记录；返回执行上下文或 None"""
        from app.models.agent import Agent
        from app.models.user import User
        from app.services.agent_access import can_execute_agent
        from app.services.session_service import SessionService

        db = SessionLocal()
        try:
            job = db.query(ScheduledJob).filter(ScheduledJob.id == job_id).first()
            if not job:
                return None
            if not job.enabled and not force:
                return None

            user = db.query(User).filter(User.id == job.user_id).first()
            agent = db.query(Agent).filter(Agent.id == job.agent_id).first()
            now = datetime.now()

            def _skip(reason: str):
                db.add(ScheduledJobRun(
                    job_id=job.id, trigger_type=trigger_type, attempt=attempt,
                    status="skipped", error=reason, started_at=now, finished_at=now))
                job.last_run_at = now
                job.last_status = "skipped"
                db.commit()
                logger.info(f"[scheduler] 任务 {job.id} 跳过: {reason}")

            if not user or not user.is_active:
                _skip("任务所属用户不存在或已禁用")
                return None
            if not agent or not agent.is_active:
                _skip("任务绑定的 Agent 不存在或已停用")
                return None
            # Agent 访问权限复查（创建任务后权限可能被回收）
            if not can_execute_agent(db, user.id, agent.id, user.role):
                _skip("用户已失去该 Agent 的执行权限")
                return None
            # 迟到策略：宽限窗口内的迟到触发，任务级可选跳过
            if not force and trigger_type in ("cron", "once") and job.next_run_at:
                late = (now - job.next_run_at).total_seconds()
                if late > _LATE_TOLERANCE_SECONDS and job.misfire_policy == "skip":
                    _skip(f"迟到 {int(late)} 秒触发，按任务策略跳过")
                    return None
            # 并发策略：上一轮未结束
            if job.skip_if_running:
                running = (db.query(ScheduledJobRun)
                           .filter(ScheduledJobRun.job_id == job.id,
                                   ScheduledJobRun.status == "running").count())
                if running > 0:
                    _skip("上一轮仍在运行，按并发策略跳过")
                    return None

            # user/agent 之后以游离对象进入执行链路：先 expunge 脱离会话，
            # 避免下面的 commit 使其属性过期，跨会话访问抛 DetachedInstanceError
            db.expunge(user)
            db.expunge(agent)

            # 取/建当日共享会话 + 落指令消息 + 建 run 记录
            prompt_rendered = render_prompt(job.prompt or "", now)
            session_service = SessionService(db)
            session = self._resolve_day_session(
                db, session_service, user, agent, job.name, now)
            session_service.add_message(
                session_id=session.id, role="user", content=prompt_rendered)
            run = ScheduledJobRun(
                job_id=job.id, session_id=session.id, trigger_type=trigger_type,
                attempt=attempt, status="running", started_at=now)
            db.add(run)
            job.last_run_at = now
            db.commit()

            return {
                "job_id": job.id, "run_id": run.id, "session_id": session.id,
                "name": job.name, "user_id": user.id, "user_role": user.role,
                "user_email": user.email, "agent_model": agent,
                "prompt_rendered": prompt_rendered, "model": job.model,
                "selected_skills": job.selected_skills, "selected_mcps": job.selected_mcps,
                "timeout_minutes": job.timeout_minutes or 0,
                "max_retries": job.max_retries or 0,
                "retry_interval_minutes": job.retry_interval_minutes or 5,
                "trigger_type": job.trigger_type, "attempt": attempt,
                "notify_on": job.notify_on or "none",
                "notify_channels": job.notify_channels or [],
            }
        except Exception as e:
            db.rollback()
            logger.exception(f"[scheduler] 任务 {job_id} 准备阶段失败")
            # 准备失败也留痕（无会话）
            try:
                db.add(ScheduledJobRun(
                    job_id=job_id, trigger_type=trigger_type, attempt=attempt,
                    status="failed", error=f"准备阶段失败: {e}",
                    started_at=datetime.now(), finished_at=datetime.now()))
                db.commit()
            except Exception:
                db.rollback()
            return None
        finally:
            db.close()

    async def _execute(self, ctx: dict):
        """启动 Agent 回合并等待终态；返回 (status, error, reply_summary)"""
        import time as _time

        # 延迟导入：chat 模块较重，且避免与本模块形成顶层循环依赖
        from app.api.v1.chat import _start_agent_task
        from app.services.output_service import get_session_output_dir
        from app.services.skill_service import SkillService
        from app.services.task_manager import task_manager

        # Fixed snapshots are authorized immediately before execution.
        db = SessionLocal()
        try:
            from app.models.user import User
            from app.models.skill import Skill
            from app.models.scheduled_job import ScheduledJob
            from app.services.skill_hub.runtime import job_versions
            from app.services.agent_versions import for_session
            from app.services.skill_runtime import prepare_agent
            job_user = db.get(User, ctx["user_id"])
            job = db.get(ScheduledJob, ctx["job_id"])
            if not job_user or not job_user.is_active:
                raise RuntimeError('任务用户不存在或已停用')
            versions = job_versions(db, job, job_user)
            frozen = for_session(db, ctx["agent_model"], ctx["session_id"])
            frozen.config['_scheduled_skill_versions'] = versions
            selected_skills = [db.get(Skill, int(key)).name for key in versions]
            ctx["agent_model"] = prepare_agent(db, frozen, selected_skills, job_user.role, job_user.id)
            db.commit()
        finally:
            db.close()

        output_dir = get_session_output_dir(ctx["user_id"], ctx["session_id"])
        _start_agent_task(
            session_id=ctx["session_id"],
            agent_model=ctx["agent_model"],
            agent_content=ctx["prompt_rendered"],
            user_role=ctx["user_role"],
            selected_skills=selected_skills,
            selected_mcps=ctx["selected_mcps"],
            user_id=ctx["user_id"],
            model=ctx["model"],
            output_dir=output_dir,
        )

        timeout_s = ctx["timeout_minutes"] * 60 if ctx["timeout_minutes"] else None
        deadline = _time.monotonic() + timeout_s if timeout_s else None
        while True:
            if not task_manager.is_running(ctx["session_id"]):
                break
            if deadline and _time.monotonic() >= deadline:
                logger.warning(f"[scheduler] 任务 {ctx['job_id']} 超时"
                               f"（>{ctx['timeout_minutes']} 分钟），终止会话 {ctx['session_id']}")
                await task_manager.cancel(ctx["session_id"], timeout=20)
                return "timeout", f"执行超时（超过 {ctx['timeout_minutes']} 分钟），已终止", \
                    self._last_reply(ctx["session_id"])
            await asyncio.sleep(2)

        info = task_manager.get(ctx["session_id"])
        raw_status = info.status if info else "done"  # 任务缓存被清理 = 已正常结束
        summary = self._last_reply(ctx["session_id"])
        if raw_status == "done":
            return "success", None, summary
        if raw_status == "stopped":
            return "failed", "任务被中断", summary
        return "failed", "Agent 执行出错（详见会话）", summary

    @staticmethod
    def _last_reply(session_id: int) -> str:
        from app.models.message import Message
        db = SessionLocal()
        try:
            row = (db.query(Message)
                   .filter(Message.session_id == session_id, Message.role == "agent")
                   .order_by(Message.id.desc()).first())
            return (row.content or "")[:500] if row else ""
        except Exception:
            return ""
        finally:
            db.close()

    async def _finalize(self, ctx: dict, status: str, error: Optional[str],
                        summary: str):
        """收尾：更新 run/任务冗余字段、一次性任务失效、安排重试、发通知"""
        started = None
        db = SessionLocal()
        try:
            run = (db.query(ScheduledJobRun)
                   .filter(ScheduledJobRun.id == ctx["run_id"]).first())
            job = (db.query(ScheduledJob)
                   .filter(ScheduledJob.id == ctx["job_id"]).first())
            if run:
                run.status = status
                run.error = error
                run.reply_summary = (summary or "")[:200] or None
                run.finished_at = datetime.now()
                started = run.started_at
            # 重试决策（先算好，一次性任务"还要重试"时保持启用）
            will_retry = (status in ("failed", "timeout")
                          and ctx["attempt"] - 1 < ctx["max_retries"])
            if job:
                job.last_status = status
                if job.trigger_type == "once" and not will_retry:
                    job.enabled = False
                    job.next_run_at = None
                    self._remove_ap_job(job.id)
                else:
                    job.next_run_at = compute_next_run(
                        job.trigger_type, job.cron_expr, job.run_at, job.timezone)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(f"[scheduler] 任务 {ctx['job_id']} 收尾落库失败")
        finally:
            db.close()

        if will_retry and self.running:
            interval = max(1, ctx["retry_interval_minutes"])
            retry_at = datetime.now() + timedelta(minutes=interval)
            try:
                self.scheduler.add_job(
                    _ap_fire, DateTrigger(run_date=retry_at),
                    args=[ctx["job_id"], "retry", ctx["attempt"] + 1],
                    id=f"{job_key(ctx['job_id'])}_retry_{ctx['run_id']}",
                    replace_existing=True)
                logger.info(f"[scheduler] 任务 {ctx['job_id']} 将于 {retry_at:%H:%M:%S} "
                            f"重试（第 {ctx['attempt'] + 1} 次）")
            except Exception:
                logger.exception(f"[scheduler] 任务 {ctx['job_id']} 安排重试失败")

        await self._notify(ctx, status, error, summary, started)

    async def _notify(self, ctx: dict, status: str, error: Optional[str],
                      summary: str, started: Optional[datetime]):
        """按任务配置发站内通知/邮件（尽力而为，失败仅记日志）"""
        if ctx["notify_on"] == "none":
            return
        if ctx["notify_on"] == "failure" and status == "success":
            return
        ok = status == "success"
        title = f"定时任务「{ctx['name']}」{_STATUS_ZH.get(status, status)}"
        dur = int((datetime.now() - started).total_seconds()) if started else None
        digest = (summary or error or "无摘要")[:200]
        link = f"/chat/{ctx['session_id']}"
        channels = ctx["notify_channels"] or []

        if "inapp" in channels:
            db = SessionLocal()
            try:
                db.add(Notification(
                    user_id=ctx["user_id"], type="scheduled_job_result", title=title,
                    content=digest, link=link,
                    job_id=ctx["job_id"], run_id=ctx["run_id"]))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("[scheduler] 站内通知写入失败")
            finally:
                db.close()

        if "email" in channels and ctx.get("user_email"):
            body = (f"任务：{ctx['name']}\n"
                    f"状态：{_STATUS_ZH.get(status, status)}\n"
                    f"触发：{_TRIGGER_ZH.get(ctx['trigger_type'], ctx['trigger_type'])}"
                    f"（第 {ctx['attempt']} 次）\n"
                    f"耗时：{dur if dur is not None else '?'} 秒\n"
                    f"摘要：{digest}\n\n"
                    f"查看完整会话：{settings.FRONTEND_BASE_URL.rstrip('/')}{link}\n")
            try:
                from app.core.mailer import send_mail
                await asyncio.to_thread(send_mail, ctx["user_email"], title, body)
            except Exception as e:
                logger.warning(f"[scheduler] 通知邮件发送失败: {e}")


# 全局单例
scheduler_service = SchedulerService()


async def _ap_fire(job_id: int, trigger_type: str, attempt: int):
    """APScheduler 回调入口（协程任务）"""
    await scheduler_service.fire(job_id, trigger_type, attempt)
