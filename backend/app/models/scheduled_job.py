"""调度任务（定时作业）模型：任务定义 + 执行记录。

独立模块：表名 scheduled_jobs / scheduled_job_runs，自包含可独立使用。
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import JSON

from app.database import Base


class ScheduledJob(Base):
    """定时任务定义（唯一事实源；APScheduler 注册项启动时从本表重建）

    权限模型：
    - 管理员可创建公共任务（is_public=True, parent_id 为 NULL）：全员可见、可被选择；
      也可创建个人任务（is_public=False, parent_id 为 NULL）：仅自己可见、不可被选择；
    - 普通用户选择公共任务后生成个人实例（is_public=False, parent_id 指向公共任务，
      字段为选择时快照），以自己身份运行、结果通知本人；
    - deleted_at 非空 = 软删除（回收站），不参与调度与常规列表，属主或管理员可恢复/彻底删除。
    """
    __tablename__ = "scheduled_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    # NULL=管理员创建的公共任务；非 NULL=用户选择公共任务生成的个人实例
    parent_id = Column(Integer, ForeignKey("scheduled_jobs.id"), nullable=True, index=True)
    # True=公共任务（全员可见、可被"选择"）；False=个人任务/用户实例（仅属主可见）
    is_public = Column(Boolean, nullable=False, default=True)

    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    # 发给 Agent 的指令文本，支持 {{date}} {{time}} {{datetime}} {{yesterday}} {{weekday}} 变量
    prompt = Column(Text, nullable=False)

    # 触发：cron（5 段 crontab 周期）| once（一次性定点）
    trigger_type = Column(String(16), nullable=False, default="cron")
    cron_expr = Column(String(64), nullable=True)
    run_at = Column(DateTime, nullable=True)
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")

    # 执行参数：空=沿用默认（Agent 模型 / 角色全部技能 / Agent 绑定的全部 MCP）
    model = Column(String(100), nullable=True)
    selected_skills = Column(JSON, nullable=True)
    selected_mcps = Column(JSON, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True)
    # 上一轮未结束时跳过本轮（默认）；关闭则允许并行
    skip_if_running = Column(Boolean, nullable=False, default=True)
    timeout_minutes = Column(Integer, nullable=False, default=60)   # 0 = 不限制
    max_retries = Column(Integer, nullable=False, default=0)        # 失败/超时后重试次数
    retry_interval_minutes = Column(Integer, nullable=False, default=5)
    misfire_policy = Column(String(16), nullable=False, default="run_once")  # run_once | skip

    # 结果通知：不通知 / 仅失败 / 每次；渠道 inapp | email
    notify_on = Column(String(16), nullable=False, default="none")
    notify_channels = Column(JSON, nullable=True)

    # 冗余展示字段（调度器维护）
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(16), nullable=True)
    next_run_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)
    # 软删除标记：非空即已移入回收站（仅管理员可见，可恢复/彻底删除）
    deleted_at = Column(DateTime, nullable=True)


class ScheduledJobRun(Base):
    """单次执行记录（定时/手动/重试各一条，可回溯到自动创建的会话）"""
    __tablename__ = "scheduled_job_runs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("scheduled_jobs.id"), nullable=False, index=True)
    # 自动创建的会话（不做硬外键：会话删除不影响执行历史）
    session_id = Column(Integer, nullable=True)
    trigger_type = Column(String(16), nullable=False)   # cron | once | manual | retry
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="running")  # running|success|failed|skipped|timeout
    error = Column(Text, nullable=True)
    reply_summary = Column(Text, nullable=True)         # Agent 最终回复前 200 字
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)
