"""站内通知模型（当前仅承载调度任务执行结果，type 字段预留扩展）"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String(32), nullable=False, default="scheduled_job_result")
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)
    link = Column(String(300), nullable=True)   # 前端路由，如 /chat/123
    job_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
