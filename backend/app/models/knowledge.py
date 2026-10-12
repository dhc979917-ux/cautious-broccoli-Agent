from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from sqlalchemy.orm import relationship
from app.database import Base


'''知识库：库（knowledge_bases）+ 文档（knowledge_documents）+ 链接（knowledge_links）。
admin 创建/管理；全员可被 Agent 绑定检索。
vault_dir 非空 = Obsidian vault 型库（md 进图谱+向量，Obsidian 可共编）；
为空 = 上传型库（pdf/docx 进向量）。
个人库（scope=personal）仅属主+管理员可见/管理。'''


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    embedding_model = Column(String(100), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # personal（个人库，仅属主 + 管理员可见/管理）| shared（共享库，全员可见、可被 Agent 绑定检索）
    scope = Column(String(20), default="shared", nullable=False)
    kind = Column(String(20), default="normal", nullable=False)
    # Obsidian vault 目录绝对路径（null=上传型；有值=vault 型，md 笔记在此目录，Obsidian 可共编）
    vault_dir = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    documents = relationship("KnowledgeDocument", back_populates="kb", cascade="all, delete-orphan")
    links = relationship("KnowledgeLink", back_populates="kb", cascade="all, delete-orphan",
                         foreign_keys="KnowledgeLink.kb_id")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True, index=True)
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)      # 原始文件名 / 笔记名
    stored_name = Column(String(255), nullable=True)    # 落盘文件名（knowledge/{kb_id}/ 下）；vault md 为空
    size = Column(Integer, default=0)
    # uploading -> parsing -> indexing -> ready / failed
    status = Column(String(20), default="uploading")
    # 同一次上传的文档批次，用于进度流和批量取消
    upload_batch_id = Column(String(32), index=True)
    chunk_count = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())

    # ---- Obsidian vault 笔记元数据（vault md 才填；上传件为空）----
    title = Column(String(255), nullable=True)                # 取自首个 H1 或 frontmatter title
    vault_path = Column(Text, nullable=True)                  # vault 内相对路径（含子目录）
    frontmatter_json = Column(Text, nullable=True)            # 解析后的 YAML（JSON 字符串）
    tags_json = Column(Text, nullable=True)                   # ["tag1","tag2"]
    category = Column(String(100), nullable=True)             # 自动分类结果（可空，需人工确认）
    content_hash = Column(String(64), nullable=True)          # 增量索引依据（md5）
    indexed_hash = Column(String(64), nullable=True)
    indexed_at = Column(DateTime(timezone=True), nullable=True)
    source_doc_id = Column(Integer, ForeignKey("knowledge_documents.id"), nullable=True)
    is_markdown = Column(Boolean, default=False)              # md 笔记才进图谱

    kb = relationship("KnowledgeBase", back_populates="documents")


class KnowledgeLink(Base):
    """文档间链接边：[[wikilink]] / 相对链接 / ![[嵌入]] / 标签引用。
    dst_id 为 null 表示 ghost 节点（目标笔记尚未创建）。"""
    __tablename__ = "knowledge_links"

    id = Column(Integer, primary_key=True, index=True)
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    src_id = Column(Integer, ForeignKey("knowledge_documents.id"), nullable=False, index=True)
    dst_id = Column(Integer, ForeignKey("knowledge_documents.id"), nullable=True, index=True)  # null=ghost
    dst_name = Column(String(255), nullable=False)            # 目标笔记名/别名（去 .md、去锚点）
    kind = Column(String(20), nullable=False)                 # link | embed | tag
    anchor = Column(String(255), nullable=True)              # [[目标#锚点]] 的锚点
    context_snippet = Column(Text, nullable=True)            # 链接所在上下文片段

    kb = relationship("KnowledgeBase", back_populates="links", foreign_keys=[kb_id])


class KnowledgeIndexJob(Base):
    __tablename__ = "knowledge_index_jobs"

    id = Column(Integer, primary_key=True)
    kb_id = Column(Integer, nullable=False, index=True)
    doc_id = Column(Integer, nullable=False, index=True)
    action = Column(String(20), nullable=False)
    revision = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now)


class MemoryArchive(Base):
    __tablename__ = "memory_archives"

    id = Column(Integer, primary_key=True)
    kb_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    source_rel = Column(Text, nullable=False)
    archive_rel = Column(Text, nullable=False)
    digest_rel = Column(Text, nullable=False)
    source_hash = Column(String(64), nullable=False)
    absorbed_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    state = Column(String(20), nullable=False, default="archived")
