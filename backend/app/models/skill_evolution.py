"""Isolated, durable personal-skill learning state; no changes to legacy tables."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class SkillEvolutionPreference(Base):
    __tablename__ = 'skill_evolution_preferences'
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    paused_resources = Column(JSON, nullable=False, default=list)


class SkillEvolutionBinding(Base):
    __tablename__ = 'skill_evolution_bindings'
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    source_resource_id = Column(String(36), ForeignKey('skill_hub_resources.id', ondelete='CASCADE'), nullable=False)
    target_resource_id = Column(String(36), ForeignKey('skill_hub_resources.id', ondelete='CASCADE'), nullable=False)
    __table_args__ = (UniqueConstraint('user_id', 'source_resource_id', name='uq_skill_evolution_binding'),)


class SkillEvolutionJob(Base):
    __tablename__ = 'skill_evolution_jobs'
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    message_id = Column(Integer, ForeignKey('messages.id', ondelete='CASCADE'), nullable=False, unique=True)
    model = Column(String(200), nullable=False)
    status = Column(String(32), nullable=False, default='queued', index=True)
    decision = Column(String(32), nullable=True)
    reason = Column(Text, nullable=False, default='')
    selected_skills = Column(JSON, nullable=False, default=list)
    runtime_versions = Column(JSON, nullable=False, default=dict)
    source_resource_id = Column(String(36), nullable=True)
    source_version_id = Column(String(36), nullable=True)
    target_resource_id = Column(String(36), nullable=True, index=True)
    base_version_id = Column(String(36), nullable=True)
    base_revision = Column(Integer, nullable=True)
    candidate_version_id = Column(String(36), nullable=True)
    candidate_revision = Column(Integer, nullable=True)
    proposal = Column(JSON, nullable=False, default=dict)
    report = Column(JSON, nullable=False, default=dict)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SessionSkillEvolution(Base):
    __tablename__ = 'session_skill_evolution'
    session_id = Column(Integer, ForeignKey('sessions.id', ondelete='CASCADE'), primary_key=True)
    source_skill_id = Column(Integer, ForeignKey('skills.id', ondelete='CASCADE'), primary_key=True)
    target_skill_id = Column(Integer, ForeignKey('skills.id', ondelete='CASCADE'), nullable=False)
