from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.sql import func
from datetime import datetime
from app.database import Base


'''MCP 服务配置表（仅 admin 可创建/修改/删除；启用的服务按 Agent 绑定注入工具）'''


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    # stdio：command/args/env 启动本地子进程；http：url 直连远程 MCP 服务
    type = Column(String(20), nullable=False, default="stdio")
    command = Column(String(500), nullable=True)       # stdio：如 npx / python
    args_json = Column(Text, nullable=True)            # stdio：参数列表 JSON，如 ["-y","xxx-server"]
    env_json = Column(Text, nullable=True)             # stdio：环境变量 JSON，如 {"API_KEY":"..."}
    env_rotated_at = Column(DateTime(timezone=True), nullable=True)
    url = Column(String(500), nullable=True)           # http：MCP 服务地址
    description = Column(Text, nullable=True)
    display_description = Column(Text, nullable=True)
    description_digest = Column(String(64), nullable=True)
    description_status = Column(String(16), nullable=False, default="pending", server_default="pending")
    enabled = Column(Boolean, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False, server_default="false")
    hidden = Column(Boolean, nullable=False, default=False, server_default="false")
    visibility_roles = Column(JSON, nullable=False, default=list)
    visibility_user_ids = Column(JSON, nullable=False, default=list)
    visibility_revision = Column(Integer, nullable=False, default=0, server_default="0")
    source_files = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)
