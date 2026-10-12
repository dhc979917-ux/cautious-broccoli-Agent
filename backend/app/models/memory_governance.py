"""Private memory index, immutable source evidence, reviews and replayable indexing."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class MemoryEntry(Base):
    __tablename__ = "memory_entries"
    __table_args__ = (UniqueConstraint("kb_id", "vault_path", name="uq_memory_entry_path"),)

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    doc_id = Column(Integer, ForeignKey("knowledge_documents.id"), nullable=True)
    vault_path = Column(String(600), nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default="published")
    index_status = Column(String(20), nullable=False, default="legacy_unknown")
    source_type = Column(String(32), nullable=False, default="legacy_unknown")
    source_session_id = Column(Integer, nullable=True)
    source_agent_id = Column(Integer, nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemoryFusionTask(Base):
    __tablename__ = "memory_fusion_task"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    source_type = Column(String(24), nullable=False)
    source_refs = Column(JSON, nullable=False)
    source_ciphertext = Column(Text, nullable=False)
    method_code = Column(String(64), nullable=False)
    method_revision = Column(Integer, nullable=False, default=1)
    model_id = Column(String(120), nullable=True)
    idempotency_key = Column(String(150), nullable=False, unique=True)
    status = Column(String(24), nullable=False, default="queued", index=True)
    revision = Column(Integer, nullable=False, default=0)
    requested_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    override_reason = Column(String(500), nullable=True)
    options = Column(JSON, nullable=False, default=dict)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class MemoryFusionItem(Base):
    __tablename__ = "memory_fusion_item"

    id = Column(String(36), primary_key=True)
    task_id = Column(String(36), ForeignKey("memory_fusion_task.id"), nullable=False, index=True)
    content_ciphertext = Column(Text, nullable=False)
    source_ids = Column(JSON, nullable=False)
    evidence = Column(Text, nullable=False)
    conflict_group = Column(String(80), nullable=True)
    confidence = Column(String(20), nullable=True)
    status = Column(String(24), nullable=False, default="pending")
    revision = Column(Integer, nullable=False, default=0)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    published_entry_id = Column(String(36), ForeignKey("memory_entries.id"), nullable=True)


class MemoryExtractionMethod(Base):
    __tablename__ = "memory_extraction_method"
    __table_args__ = (UniqueConstraint("code", "revision", name="uq_memory_method_revision"),)

    id = Column(String(36), primary_key=True)
    code = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False)
    title = Column(String(100), nullable=False)
    instructions_ciphertext = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemoryIndexJob(Base):
    __tablename__ = "memory_index_jobs"

    id = Column(String(36), primary_key=True)
    item_id = Column(String(36), ForeignKey("memory_fusion_item.id"), nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False)
    entry_id = Column(String(36), ForeignKey("memory_entries.id"), nullable=False)
    path = Column(String(600), nullable=False)
    content_hash = Column(String(64), nullable=False)
    action = Column(String(20), nullable=False, default='publish')
    status = Column(String(24), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    error_code = Column(String(64), nullable=True)
    lease_token = Column(String(36), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemorySourceDisposition(Base):
    __tablename__ = "memory_source_dispositions"
    __table_args__ = (UniqueConstraint("task_id", "entry_id", name="uq_memory_source_disposition"),)

    id = Column(String(36), primary_key=True)
    task_id = Column(String(36), ForeignKey("memory_fusion_task.id"), nullable=False)
    entry_id = Column(String(36), ForeignKey("memory_entries.id"), nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="pending", index=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
