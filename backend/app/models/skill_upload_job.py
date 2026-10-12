"""技能包上传作业（异步化后的状态载体）。

上传接口只做校验与暂存后立即返回 job_id，解压落盘 / 同步注册 / 中文名生成
在后台任务里推进，状态写进本表供前端轮询——避免"前端超时断开、后端其实已成功"
造成的重复上传与误删。

作业不跨进程重启存活：服务启动时残留的非终态作业统一标记 failed（见 skill_upload_service）。
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text

from datetime import datetime

from app.database import Base

# 作业状态机：pending → extracting → syncing → done / failed
JOB_PENDING = "pending"
JOB_EXTRACTING = "extracting"
JOB_SYNCING = "syncing"
JOB_DONE = "done"
JOB_FAILED = "failed"
TERMINAL_STATUSES = (JOB_DONE, JOB_FAILED)


class SkillUploadJob(Base):
    __tablename__ = "skill_upload_jobs"

    id = Column(Integer, primary_key=True, index=True)
    package_name = Column(String(200), nullable=False)     # 落盘后的一级目录名
    kind = Column(String(10), nullable=False, default="folder")   # folder / zip
    status = Column(String(20), nullable=False, default=JOB_PENDING, index=True)
    message = Column(String(500), nullable=True)           # 面向用户的进度/结果说明
    error = Column(Text, nullable=True)                    # 失败原因（含堆栈摘要）
    file_count = Column(Integer, nullable=False, default=0)
    total_bytes = Column(BigInteger, nullable=False, default=0)
    new_skills_json = Column(Text, nullable=True)          # JSON 数组：本次新注册的技能名
    stats_json = Column(Text, nullable=True)               # JSON：本次同步统计
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # 用 Python 侧本地时间入库（与 notifications/scheduled_jobs 一致）：
    # server_default=func.now() 在 SQLite 下写的是 UTC，与超时回收用的 datetime.now()
    # 相差一个时区，会把刚受理的作业误判成"超期未完结"直接回收掉。
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
