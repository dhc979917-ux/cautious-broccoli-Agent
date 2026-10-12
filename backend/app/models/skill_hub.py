"""Personal Skill resources and community relations, separate from system governance."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class SkillHubResource(Base):
    __tablename__ = 'skill_hub_resources'
    id = Column(String(36), primary_key=True)
    kind = Column(String(16), nullable=False)
    skill_id = Column(Integer, ForeignKey('skills.id', ondelete='SET NULL'), nullable=True, unique=True)
    package_id = Column(Integer, ForeignKey('skill_packages.id', ondelete='SET NULL'), nullable=True, unique=True)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    origin = Column(String(20), nullable=False, default='system')
    title = Column(String(200), nullable=False)
    summary = Column(Text, nullable=False, default='')
    allow_fork = Column(Boolean, nullable=False, default=False)
    archived = Column(Boolean, nullable=False, default=False)
    blocked = Column(Boolean, nullable=False, default=False)
    draft_version_id = Column(String(36), nullable=True)
    published_version_id = Column(String(36), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    source_resource_id = Column(String(36), nullable=True)
    source_version_id = Column(String(36), nullable=True)
    source_title = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillHubVersion(Base):
    __tablename__ = 'skill_hub_versions'
    id = Column(String(36), primary_key=True)
    resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False, index=True)
    skill_version_id = Column(String(36), ForeignKey('skill_versions.id'), nullable=True)
    package_version_id = Column(String(36), ForeignKey('skill_package_versions.id'), nullable=True)
    number = Column(Integer, nullable=False)
    state = Column(String(20), nullable=False, default='draft')
    test_status = Column(String(20), nullable=False, default='untested')
    test_report = Column(JSON, nullable=False, default=dict)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint('resource_id', 'number', name='uq_skillhub_version_number'),)


class SkillHubFork(Base):
    __tablename__ = 'skill_forks'
    id = Column(Integer, primary_key=True)
    source_resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False)
    source_version_id = Column(String(36), nullable=False)
    forked_resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False, unique=True)
    forked_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    source_title = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillHubShare(Base):
    __tablename__ = 'skill_shares'
    id = Column(String(36), primary_key=True)
    resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False)
    shared_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    shared_to = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    version_id = Column(String(36), nullable=False)
    proposed_version_id = Column(String(36), nullable=True)
    allow_fork = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint('resource_id', 'shared_to', name='uq_skillhub_share_recipient'),)


class SkillMarketplaceListing(Base):
    __tablename__ = 'skill_marketplace_listings'
    id = Column(String(36), primary_key=True)
    resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False, unique=True)
    author_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    version_id = Column(String(36), nullable=True)
    status = Column(String(24), nullable=False, default='draft', index=True)
    details = Column(JSON, nullable=False, default=dict)
    review_note = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillMarketplaceRequest(Base):
    __tablename__ = 'skill_marketplace_requests'
    id = Column(String(36), primary_key=True)
    listing_id = Column(String(36), ForeignKey('skill_marketplace_listings.id'), nullable=False, index=True)
    version_id = Column(String(36), nullable=False)
    submitted_by = Column(Integer, ForeignKey('users.id'), nullable=False)
    details = Column(JSON, nullable=False)
    status = Column(String(24), nullable=False, default='pending_review', index=True)
    reviewer_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    review_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class SkillHubInstall(Base):
    __tablename__ = 'skill_installs'
    id = Column(String(36), primary_key=True)
    listing_id = Column(String(36), ForeignKey('skill_marketplace_listings.id'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False, unique=True)
    installed_version_id = Column(String(36), nullable=False)
    local_version_id = Column(String(36), nullable=True)
    uninstalled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillHubReview(Base):
    __tablename__ = 'skill_reviews'
    id = Column(String(36), primary_key=True)
    listing_id = Column(String(36), ForeignKey('skill_marketplace_listings.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=False, default='')
    version_id = Column(String(36), nullable=False)
    author_reply = Column(Text, nullable=True)
    hidden = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint('listing_id', 'user_id', name='uq_skillhub_review_user'),)


class SkillHubReport(Base):
    __tablename__ = 'skill_hub_reports'
    id = Column(String(36), primary_key=True)
    listing_id = Column(String(36), ForeignKey('skill_marketplace_listings.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='open')
    resolution = Column(Text, nullable=True)
    reviewer_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SkillHubBlock(Base):
    __tablename__ = 'skill_hub_blocks'
    id = Column(Integer, primary_key=True)
    resource_id = Column(String(36), ForeignKey('skill_hub_resources.id'), nullable=False, index=True)
    version_id = Column(String(36), nullable=True)
    reason = Column(Text, nullable=False)
    actor_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScheduledSkillBinding(Base):
    __tablename__ = 'scheduled_skill_bindings'
    job_id = Column(Integer, ForeignKey('scheduled_jobs.id', ondelete='CASCADE'), primary_key=True)
    versions = Column(JSON, nullable=False, default=dict)
    revision = Column(Integer, nullable=False, default=1)
