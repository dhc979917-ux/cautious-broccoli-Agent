from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class GovernancePolicy(Base):
    __tablename__ = 'governance_policies'
    __table_args__ = (UniqueConstraint('resource', 'scope_type', 'scope_id', name='uq_governance_policy_scope'),)

    id = Column(Integer, primary_key=True)
    resource = Column(String(40), nullable=False)
    scope_type = Column(String(20), nullable=False, default='global')
    scope_id = Column(Integer, nullable=False, default=0)
    retention_days = Column(Integer, nullable=False, default=365)
    backlog_threshold = Column(Integer, nullable=False, default=100)
    responsible_user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GovernanceCleanup(Base):
    __tablename__ = 'governance_cleanup_requests'

    id = Column(String(36), primary_key=True)
    resource = Column(String(40), nullable=False)
    cutoff = Column(DateTime(timezone=True), nullable=False)
    targets = Column(JSON, nullable=False)
    requested_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    reviewed_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    reason = Column(Text, nullable=False)
    review_reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default='pending')
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
