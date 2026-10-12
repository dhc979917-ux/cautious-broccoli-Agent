from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, Text

from app.database import Base


'''会话上下文持久化：Agent 记忆按会话落库。
Agent 实例常驻进程内存，但进程重启（uvicorn --reload / 容器重建）会丢内存上下文；
本表在每轮对话结束时写回 Agent 的 context+summary（state_json），
Agent 重建时按 session_id 灌回，实现跨重启上下文不丢。
行随会话删除清理（见 agent_manager.evict_session）。'''


class SessionContext(Base):
    __tablename__ = "session_context"

    session_id = Column(Integer, primary_key=True)          # 对应 sessions.id
    state_json = Column(Text, nullable=False)               # {"v":1,"summary":...,"context":[Msg...]}
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
