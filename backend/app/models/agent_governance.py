from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "number", name="uq_agent_version_number"),)

    id = Column(String(36), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False, default="draft")
    snapshot = Column(JSON, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    test_status = Column(String(16), nullable=False, default="untested")
    test_summary = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentPublication(Base):
    __tablename__ = "agent_publications"

    agent_id = Column(Integer, ForeignKey("agents.id"), primary_key=True)
    version_id = Column(String(36), ForeignKey("agent_versions.id"), nullable=False)
    revision = Column(Integer, nullable=False, default=0)
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentReleaseEvent(Base):
    __tablename__ = "agent_release_events"

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    previous_version_id = Column(String(36), ForeignKey("agent_versions.id"), nullable=True)
    version_id = Column(String(36), ForeignKey("agent_versions.id"), nullable=False)
    action = Column(String(16), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SessionAgentVersion(Base):
    __tablename__ = "session_agent_versions"

    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    version_id = Column(String(36), ForeignKey("agent_versions.id"), nullable=False)
    pinned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
