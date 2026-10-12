from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.database import Base


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id = Column(String(36), primary_key=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    source_rel_path = Column(String(500), nullable=False)
    snapshot_rel_path = Column(String(200), nullable=False, unique=True)
    manifest = Column(JSON, nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="draft")
    dependencies = Column(JSON, nullable=False, default=list)
    category = Column(String(80), nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    test_status = Column(String(16), nullable=False, default="untested")
    test_summary = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillPublication(Base):
    __tablename__ = "skill_publications"

    skill_id = Column(Integer, ForeignKey("skills.id"), primary_key=True)
    version_id = Column(String(36), ForeignKey("skill_versions.id"), nullable=False)
    revision = Column(Integer, nullable=False, default=0)
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillReleaseEvent(Base):
    __tablename__ = "skill_release_events"

    id = Column(Integer, primary_key=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    previous_version_id = Column(String(36), ForeignKey("skill_versions.id"), nullable=True)
    version_id = Column(String(36), ForeignKey("skill_versions.id"), nullable=False)
    action = Column(String(16), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
