from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class McpToolCatalog(Base):
    __tablename__ = "mcp_tool_catalog"
    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_mcp_tool_server_name"),)

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("mcp_servers.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    display_description = Column(Text, nullable=True)
    description_digest = Column(String(64), nullable=True)
    description_status = Column(String(16), nullable=False, default="pending", server_default="pending")
    input_schema = Column(JSON, nullable=False)
    output_schema = Column(JSON, nullable=True)
    schema_digest = Column(String(64), nullable=False)
    risk = Column(String(16), nullable=False, default="unknown")
    available = Column(Boolean, nullable=False, default=True)
    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revision = Column(Integer, nullable=False, default=0)


class AgentToolGrant(Base):
    __tablename__ = "agent_tool_grants"
    __table_args__ = (UniqueConstraint("agent_id", "server_id", "tool_name", name="uq_agent_tool_grant"),)

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    server_id = Column(Integer, ForeignKey("mcp_servers.id"), nullable=False)
    tool_name = Column(String(200), nullable=False)
    schema_digest = Column(String(64), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ToolApproval(Base):
    __tablename__ = "tool_approvals"
    __table_args__ = (UniqueConstraint("session_id", "tool_call_id", name="uq_tool_approval_call"),)

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)
    tool_call_id = Column(String(100), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    server_id = Column(Integer, ForeignKey("mcp_servers.id"), nullable=False)
    tool_name = Column(String(200), nullable=False)
    arguments_digest = Column(String(64), nullable=False)
    schema_digest = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    audit_event_id = Column(Integer, ForeignKey("audit_events.id"), nullable=False)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
