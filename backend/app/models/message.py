from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from datetime import datetime
from sqlalchemy.orm import relationship
from app.database import Base


'''消息表'''


class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    # 外键指向 sessions 表的 id，和 session.py 的 relationship 形成双向关联
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    role = Column(String(20), nullable=False)  # 消息角色: user, agent, system
    content = Column(Text, nullable=False) # 消息内容
    agent_name = Column(String(100), nullable=True)
    # - 消息的附加元数据，JSON 格式的字符串
    # - agent 消息: {"steps":[...], "file_outputs":[...], "token_usage":{...}, "tool_call_count":N}
    # - user 消息:  {"attachments":[{"name":..., "stored_name":...}]}
    metadata_json = Column(Text, nullable=True)  # JSON格式的元数据
    # 统一北京时间（func.now() 在 SQLite 里是 UTC，会差 8 小时，改用 Python 本地时间）
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    
    # 关系
    session = relationship("Session", back_populates="messages")
