from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class PackageVersion(Base):
    __tablename__ = 'skill_package_versions'
    __table_args__ = (UniqueConstraint('package_id', 'number', name='uq_package_version_number'),)

    id = Column(String(36), primary_key=True)
    package_id = Column(Integer, ForeignKey('skill_packages.id', ondelete='CASCADE'), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    snapshot_rel_path = Column(String(240), nullable=False, unique=True)
    manifest = Column(JSON, nullable=False)
    content_hash = Column(String(64), nullable=False)
    members = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PackagePublication(Base):
    __tablename__ = 'skill_package_publications'

    package_id = Column(Integer, ForeignKey('skill_packages.id', ondelete='CASCADE'), primary_key=True)
    version_id = Column(String(36), ForeignKey('skill_package_versions.id'), nullable=False)
    revision = Column(Integer, nullable=False, default=1)


class ResourceChangeJob(Base):
    __tablename__ = 'skill_resource_change_jobs'

    id = Column(String(36), primary_key=True)
    resource_type = Column(String(16), nullable=False)
    resource_id = Column(Integer, nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    operation = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default='staged', index=True)
    message = Column(String(500), nullable=False, default='已受理')
    source_rel_path = Column(String(500), nullable=False)
    staging_name = Column(String(100), nullable=False)
    backup_name = Column(String(100), nullable=False)
    base_version_id = Column(String(36), nullable=False)
    base_revision = Column(Integer, nullable=False)
    plan = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
