from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from datetime import datetime
from sqlalchemy.orm import relationship
from app.database import Base


'''会话表'''


class Session(Base):
    __tablename__ = "sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    # 这个会话属于哪个用户。ForeignKey("users.id") 是外键约束，指向 users 表的 id 列
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # 这个会话是和哪个 Agent 进行的。指向 agents 表的 id
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    # 会话标题，默认"新对话"。实际产品中可以取用户第一句话的前20个字作为标题
    title = Column(String(200), default="新对话")
    status = Column(String(20), default="active")  # 会话状态，active（进行中）或 closed（已关闭）
    # 会话上下文，JSON 类型
    # 用于保存对话的上下文信息（比如多轮对话的摘要、关键信息提取等）
    # 当前代码里没有用到，预留给未来的上下文管理功能
    context = Column(JSON, nullable=True)
    # 统一北京时间（func.now() 在 SQLite 里是 UTC，会差 8 小时，改用 Python 本地时间）
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)
    
    #SQLAlchemy 的关系定义：
    #   - relationship("Message")：告诉 SQLAlchemy，一个 Session 关联多个 Message
    #   - back_populates="session"：和 Message 表里的 session 字段互相关联（双向绑定）
    #   - cascade="all, delete-orphan"：级联删除。当一个会话被删除时，它下面的所有消息也会被自动删除。"orphan" 指的是如果某条消息和会话的关联被断开
    #     了，这条消息也会被删除
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
