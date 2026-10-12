from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


'''
权限表：多对多关联表，连接 User 和 Agent
'''


class UserAgentAccess(Base):
    __tablename__ = "user_agent_access"

    # 一条记录 = "用户 X 可以使用 Agent Y"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)

    # 权限级别：
    #   - execute：可以使用（和Agent对话）
    #   - read：只能查看Agent信息，不能对话
    #   - admin：可以管理这个Agent（修改配置等）
    access_level = Column(String(20), default="execute")  # execute, read, admin
    granted_at = Column(DateTime(timezone=True), server_default=func.now()) # 权限授予时间
    
    # 唯一约束：一个用户对一个Agent只有一条权限记录
    __table_args__ = (
        {"sqlite_autoincrement": True},
    )
