"""知识库服务：KB/文档 CRUD + 解析入库管线 + 检索。

管线：上传落盘 → 按扩展名选 AgentScope 解析器（pdf/docx/xlsx/pptx/txt…）→
ApproxTokenChunker 切块 → DashScopeEmbeddingModel 向量化 → SqliteVectorStore 入库。
向量集合按 KB 隔离（collection = kb_{id}）。embedding 按调用计费，单文件限 50MB。"""

import logging

logger = logging.getLogger(__name__)

import asyncio
import hashlib
import os
from datetime import datetime
from typing import Optional, List

from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, KnowledgeLink
from app.services.knowledge.progress import (
    KnowledgeImportCancelled,
    ProgressReporter,
)
from app.services.vector_store import get_vector_store

EMBED_BATCH = 8
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
EMBED_DIMENSIONS = 1024


def _kb_dir(kb_id: int) -> str:
    d = os.path.join(settings.KNOWLEDGE_DIR, str(kb_id))
    os.makedirs(d, exist_ok=True)
    return d


def _read_doc_bytes(doc) -> Optional[bytes]:
    """读取知识库文档源文件字节（本地磁盘）。"""
    if doc.stored_name:
        p = os.path.join(_kb_dir(doc.kb_id), doc.stored_name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                return fh.read()
    return None


def _materialize_doc(doc) -> tuple:
    """把文档源文件落到可读路径 + 是否临时文件标志（ingest 等需要 path 的管线用）。"""
    if doc.stored_name:
        p = os.path.join(_kb_dir(doc.kb_id), doc.stored_name or doc.filename)
        if os.path.isfile(p):
            return p, False
    return None, False


def _delete_doc_file(doc, kb_id: int) -> None:
    """删除文档源文件（本地磁盘）。"""
    if doc.stored_name:
        p = os.path.join(_kb_dir(kb_id), doc.stored_name)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass


def _vault_dir(kb_id: int, scope: str, user_id: Optional[int] = None) -> str:
    base = settings.VAULTS_DIR
    if scope == "personal":
        path = os.path.join(base, "personal", str(user_id or 0), str(kb_id))
    else:
        path = os.path.join(base, "shared", str(kb_id))
    os.makedirs(path, exist_ok=True)
    return path

def _collection(kb_id: int) -> str:
    return f"kb_{kb_id}"


# ---- embedding 模型单例 ----
_embedding_model = None


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from app.services.loop_safe_embedding import LoopSafeOpenAIEmbeddingModel
        from app.agents.model_factory import _get_credential
        _embedding_model = LoopSafeOpenAIEmbeddingModel(
            credential=_get_credential(),
            model=settings.EMBEDDING_MODEL,
            dimensions=EMBED_DIMENSIONS,
            pass_dimensions=False,
        )
    return _embedding_model


async def embed_texts(texts: List[str]) -> List[List[float]]:
    """批量向量化（按 EMBED_BATCH 分批调用）"""
    model = get_embedding_model()
    out: List[List[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        resp = await model(inputs=batch)
        out.extend([list(e) for e in resp.embeddings])
    return out


def _get_parser(filename: str):
    from agentscope.rag import PDFParser, WordParser, ExcelParser, PPTParser, TextParser
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return PDFParser()
    if ext in (".doc", ".docx"):
        return WordParser()
    if ext in (".xls", ".xlsx", ".csv"):
        return ExcelParser()
    if ext in (".ppt", ".pptx"):
        return PPTParser()
    return TextParser()


def _chunk_text(chunk) -> str:
    """从 Chunk 提取纯文本。content 为 TextBlock（取 .text）或 DataBlock（多模态，无文本返回空串）。
    用 getattr 解耦，不直接依赖 TextBlock/DataBlock 类型。"""
    c = chunk.content
    if isinstance(c, str):
        return c
    return getattr(c, "text", "") or ""



def _get_existing_titles(db, kb_id: int) -> list:
    """Get all MD note titles in the vault for wikilink suggestions."""
    docs = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.is_markdown == True,  # noqa: E712
        KnowledgeDocument.status.notin_(("deleted", "archived")),
    ).all()
    titles = []
    for d in docs:
        if d.title:
            titles.append(d.title)
    return titles


# Convertible document extensions (non-MD formats that should go through source_to_md pipeline)
CONVERTIBLE_EXTS = {".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".ppt", ".pptx"}


def recover_stale_documents() -> None:
    """启动时把卡在中间态的文档复位为 failed。

    进程重启会打断正在执行的后台解析任务，处于 converting/parsing/indexing
    的文档将永远停在中间态；复位为 failed 让用户可在界面上重试。
    仅在应用启动时调用一次（此刻必然没有进行中的任务，不会误伤）。
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        from app.models.knowledge import KnowledgeIndexJob
        queued = {job.doc_id for job in db.query(KnowledgeIndexJob).filter(
            KnowledgeIndexJob.status.in_(("pending", "running"))
        ).all() if job.action == "ingest"}
        stale = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.status.in_(["converting", "parsing", "indexing"])
        ).all()
        for d in stale:
            if d.id in queued:
                d.status = "indexing" if d.is_markdown else "parsing"
                d.error = None
                continue
            d.status = "failed"
            d.error = "处理被中断（服务重启），请点击重试"
        if stale:
            db.commit()
            logger.info(f"[知识库] 启动自愈：{len(stale)} 个卡在中间态的文档已复位为 failed")
    except Exception as e:
        logger.warning(f"[知识库] 启动自愈失败（不影响启动）: {e}")
    finally:
        db.close()


class KnowledgeService:
    def __init__(self, db: DBSession):
        self.db = db

    # ---- KB CRUD ----
    def list_kbs(self) -> List[KnowledgeBase]:
        return self.db.query(KnowledgeBase).order_by(KnowledgeBase.id.asc()).all()

    # ---- 多用户可见性（方案B）----
    def list_kbs_visible(self, user_id: Optional[int], role: str) -> List[KnowledgeBase]:
        """当前用户可见的 KB：shared 全员可见；personal 仅属主可见（管理员也看不到）。"""
        from sqlalchemy import or_
        q = self.db.query(KnowledgeBase).filter(
            or_(KnowledgeBase.scope == "shared", KnowledgeBase.created_by == user_id)
        )
        return q.order_by(KnowledgeBase.id.asc()).all()

    def can_view(self, kb: KnowledgeBase, user_id: Optional[int], role: str) -> bool:
        if kb.scope == "personal":
            return kb.created_by == user_id  # 个人库完全隔离，管理员也不可见
        return True

    def can_manage(self, kb: KnowledgeBase, user_id: Optional[int], role: str, perm_check=None) -> bool:
        """能否对该 KB 做增删改：admin / 个人库属主 / 共享库且有 kb_manage 权限。
        perm_check: 可选的 has_perm(user, code) 回调，用于共享库细粒度权限判定。"""
        if kb.scope == "personal":
            return kb.created_by == user_id  # 个人库仅属主管理，管理员不介入
        if role == "admin":
            return True
        if kb.scope == "shared" and perm_check is not None:
            return perm_check()
        return False

    def accessible_kb_ids_for_search(self, user_id: Optional[int], admin_selected_ids: Optional[list]) -> List[int]:
        """Agent 检索（方案B）最终 KB 集合：
        管理员在 Agent.config 绑定的 KB（按访问权过滤）∪ 当前用户的全部个人库。"""
        admin_ids = [int(k) for k in (admin_selected_ids or []) if k is not None]
        out: set = set()
        if admin_ids:
            rows = self.db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(admin_ids)).all()
            for kb in rows:
                if kb.scope == "shared" or kb.created_by == user_id:
                    out.add(kb.id)
        if user_id is not None:
            own = (
                self.db.query(KnowledgeBase)
                .filter(KnowledgeBase.scope == "personal", KnowledgeBase.created_by == user_id)
                .all()
            )
            for kb in own:
                out.add(kb.id)
        return sorted(out)

    def get_kb(self, kb_id: int) -> Optional[KnowledgeBase]:
        return self.db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()

    def create_kb(self, user_id: int, name: str, description: Optional[str], scope: str = "shared") -> KnowledgeBase:
        scope = scope if scope in ("personal", "shared") else "shared"
        kb = KnowledgeBase(
            name=name.strip(),
            description=description,
            embedding_model=settings.EMBEDDING_MODEL,
            created_by=user_id,
            scope=scope,
        )
        self.db.add(kb)
        self.db.commit()
        self.db.refresh(kb)
        # Auto-create vault directory — no user-provided paths needed
        kb.vault_dir = _vault_dir(kb.id, scope, user_id)
        self.db.commit()
        return kb

    def update_kb(self, kb_id: int, data: dict) -> Optional[KnowledgeBase]:
        kb = self.get_kb(kb_id)
        if not kb:
            return None
        if "name" in data and data["name"]:
            new_name = str(data["name"]).strip()
            if new_name != kb.name:
                dup = self.db.query(KnowledgeBase).filter(
                    KnowledgeBase.name == new_name, KnowledgeBase.id != kb_id
                ).first()
                if dup:
                    raise ValueError("同名知识库已存在")
            kb.name = new_name
        if "description" in data:
            kb.description = data["description"]
        self.db.commit()
        self.db.refresh(kb)
        return kb

    def delete_kb(self, kb_id: int) -> bool:
        kb = self.get_kb(kb_id)
        if not kb:
            return False
        # 删除向量集合 + 物理文件（尽力而为，失败不阻塞）
        from app.services.knowledge.index_jobs import enqueue
        enqueue(self.db, kb_id, 0, "drop")
        try:
            import shutil
            shutil.rmtree(_kb_dir(kb_id), ignore_errors=True)
        except Exception:
            pass
        if kb.vault_dir and os.path.isdir(kb.vault_dir):
            try:
                import shutil
                shutil.rmtree(kb.vault_dir, ignore_errors=True)
            except Exception:
                pass
        self.db.delete(kb)
        self.db.commit()
        return True

    async def _drop_collection(self, kb_id: int):
        try:
            store = get_vector_store()
            if await store.has_collection(_collection(kb_id)):
                await store.delete_collection(_collection(kb_id))
        except Exception as e:
            logger.error(f"[知识库] 删除向量集合失败 kb={kb_id}: {e}")

    # ---- 文档 ----
    def list_documents(self, kb_id: int) -> List[KnowledgeDocument]:
        return (
            self.db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.kb_id == kb_id,
                    KnowledgeDocument.status != "archived")
            .order_by(KnowledgeDocument.id.desc())
            .all()
        )

    def get_document(self, doc_id: int) -> Optional[KnowledgeDocument]:
        return self.db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()

    def register_document(
        self,
        kb_id: int,
        user_id: int,
        filename: str,
        stored_name: str,
        size: int,
        upload_batch_id: Optional[str] = None,
    ) -> KnowledgeDocument:
        doc = KnowledgeDocument(
            kb_id=kb_id,
            filename=filename,
            stored_name=stored_name,
            size=size,
            status="parsing",
            created_by=user_id,
            upload_batch_id=upload_batch_id,
        )
        self.db.add(doc)
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def delete_document(self, doc_id: int) -> bool:
        doc = self.get_document(doc_id)
        if not doc:
            return False
        kb_id = doc.kb_id
        from app.services.knowledge.index_jobs import enqueue
        enqueue(self.db, kb_id, doc_id, "delete")
        try:
            _delete_doc_file(doc, kb_id)
        except Exception:
            pass
        self.db.delete(doc)
        self.db.commit()
        return True

    async def _delete_doc_vectors(self, kb_id: int, doc_id: int):
        try:
            store = get_vector_store()
            await store.delete(_collection(kb_id), str(doc_id))
        except Exception as e:
            logger.error(f"[知识库] 删除文档向量失败 doc={doc_id}: {e}")

    # ---- 入库管线（后台任务）----
    def soft_delete_document(self, doc_id: int) -> bool:
        """Soft delete: mark as deleted, keep files and vectors."""
        doc = self.get_document(doc_id)
        if not doc:
            return False
        doc.status = "deleted"
        self.db.commit()
        return True

    def restore_document(self, doc_id: int) -> bool:
        """Restore a soft-deleted document (status -> ready)."""
        doc = self.get_document(doc_id)
        if not doc:
            return False
        doc.status = "ready"
        self.db.commit()
        return True

    def hard_delete_document(self, doc_id: int) -> bool:
        """Hard delete: remove MD file + linked source PDF + vectors + DB records + links."""
        doc = self.get_document(doc_id)
        if not doc:
            return False
        kb = self.get_kb(doc.kb_id)
        kb_id = doc.kb_id

        # If this is an MD note converted from a PDF, also find and remove the source PDF
        linked_pdf = None
        if doc.is_markdown and kb and kb.vault_dir and doc.vault_path:
            try:
                import frontmatter
                md_path = os.path.join(kb.vault_dir, doc.vault_path)
                if os.path.isfile(md_path):
                    post = frontmatter.load(md_path)
                    source_file = post.get("source_file")
                    if source_file:
                        linked_pdf = (
                            self.db.query(KnowledgeDocument)
                            .filter(KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.stored_name == source_file)
                            .first()
                        )
                    # Remove the MD file from vault
                    os.remove(md_path)
            except Exception as e:
                logger.error(f"[knowledge] hard_delete MD cleanup error: {e}")

        # Fallback: if source PDF not found via frontmatter (e.g. MD file already gone),
        # match by base filename within the same KB
        if linked_pdf is None and doc.is_markdown and doc.source_doc_id:
            try:
                import re as _re
                def _norm_base(fn):
                    return _re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(fn)[0]).strip()
                md_base = _norm_base(doc.filename)
                linked_pdf = self.db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.id == doc.source_doc_id,
                    KnowledgeDocument.kb_id == kb_id,
                ).first()
            except Exception as e:
                logger.error(f"[knowledge] hard_delete fallback search error: {e}")

        # Delete vectors for this doc (and linked PDF) async
        from app.services.knowledge.index_jobs import enqueue
        enqueue(self.db, kb_id, doc_id, "delete")
        if linked_pdf:
            enqueue(self.db, kb_id, linked_pdf.id, "delete")

        # Delete linked PDF physical file + DB record
        if linked_pdf:
            try:
                _delete_doc_file(linked_pdf, kb_id)
            except Exception:
                pass
            self.db.query(KnowledgeLink).filter(
                (KnowledgeLink.src_id == linked_pdf.id) | (KnowledgeLink.dst_id == linked_pdf.id)
            ).delete(synchronize_session=False)
            self.db.delete(linked_pdf)

        # Delete physical file for non-MD docs (local)
        if not doc.is_markdown and doc.stored_name:
            try:
                _delete_doc_file(doc, kb_id)
            except Exception:
                pass

        # Delete links referencing this doc, then the doc itself
        self.db.query(KnowledgeLink).filter(
            (KnowledgeLink.src_id == doc_id) | (KnowledgeLink.dst_id == doc_id)
        ).delete(synchronize_session=False)
        self.db.delete(doc)
        self.db.commit()
        return True

    async def ingest_document(
        self,
        doc_id: int,
        progress: Optional[ProgressReporter] = None,
    ):
        """Parse document. Non-MD formats (PDF/Excel/Word/PPT) are converted to
        Obsidian-style Markdown via SourceToMdService, then indexed and vectorized.
        Text files use the existing parser pipeline."""
        from app.database import SessionLocal
        db = SessionLocal()
        path = None
        _is_temp = False
        try:
            doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
            if not doc:
                return
            if progress:
                progress.update("准备处理", 3, "读取源文件", status="parsing")
            if progress:
                progress.check_cancel()
            # md 笔记行（手写笔记/转换产物）：只做向量化，不走源文件转换管线。
            # 这样"转换成功但向量化失败"的笔记重试时不会重跑昂贵的 LLM 改写。
            if doc.is_markdown:
                await self.ingest_vault_md(doc_id, progress=progress)
                return
            path, _is_temp = _materialize_doc(doc)
            if path is None:
                doc.status = "failed"
                doc.error = "源文件不可读（本地磁盘缺失）"
                db.commit()
                return
            ext = os.path.splitext(doc.filename)[1].lower()

            if ext in CONVERTIBLE_EXTS:
                # New pipeline: convert to MD, index, vectorize
                try:
                    doc.status = "converting"
                    db.commit()
                    if progress:
                        progress.update("转换中", 10, "抽取并改写文档", status="converting")

                    from app.services.knowledge.source_to_md_service import SourceToMdService
                    from app.services.knowledge.markdown_indexer import MarkdownIndexer

                    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
                    if not kb:
                        raise ValueError("Knowledge base not found")

                    vault_dir = kb.vault_dir or _vault_dir(doc.kb_id, kb.scope or "shared", kb.created_by)
                    if not kb.vault_dir:
                        kb.vault_dir = vault_dir
                        db.commit()
                    os.makedirs(vault_dir, exist_ok=True)

                    existing_titles = _get_existing_titles(db, kb.id)

                    svc = SourceToMdService()
                    md_path = await svc.convert_to_md_async(
                        source_path=path,
                        vault_dir=vault_dir,
                       original_filename=doc.filename,
                        stored_name=doc.stored_name,
                        existing_titles=existing_titles,
                        kb_id=doc.kb_id,
                        on_progress=progress.report if progress else None,
                        should_cancel=(progress.check_cancel if progress else None),
                    )

                    # Index the new MD file (creates knowledge_documents + knowledge_links)
                    if progress:
                        progress.update("建立索引", 74, "写入图谱和笔记")
                    indexer = MarkdownIndexer(db)
                    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
                    stats = indexer.index_vault(kb)

                    # Vectorize the new MD docs
                    child_progress = progress.child(80, 100) if progress else None
                    for new_doc_id in stats.get("changed", []):
                        await self.ingest_vault_md(new_doc_id, progress=child_progress)

                    generated_docs = (
                        db.query(KnowledgeDocument)
                        .filter(KnowledgeDocument.id.in_(stats.get("changed", [])))
                        .all()
                    )
                    for generated in generated_docs:
                        if generated.vault_path == os.path.relpath(md_path, vault_dir).replace(chr(92), "/"):
                            generated.source_doc_id = doc.id
                    db.commit()
                    failed_generated = next(
                        (d for d in generated_docs if d.status == "failed"),
                        None,
                    )
                    if failed_generated is not None:
                        doc.status = "failed"
                        doc.error = failed_generated.error
                        db.commit()
                        if progress:
                            progress.report(
                                "失败",
                                100,
                                failed_generated.error or "生成笔记向量化失败",
                                status="failed",
                                error=failed_generated.error,
                            )
                        return

                    # Mark original doc as converted only after its generated note is indexed.
                    doc.status = "converted"
                    doc.error = None
                    db.commit()
                    if progress:
                        progress.update("已完成", 100, "转换和向量化完成", status="converted")
                    logger.info(f"[知识库] 文档转换完成 doc={doc.id} -> {os.path.basename(md_path)}")

                except KnowledgeImportCancelled:
                    doc.status = "canceled"
                    doc.error = None
                    db.commit()
                    if progress:
                        progress.report("已取消", 100, "用户取消了本次处理", status="canceled")
                except Exception as e:
                    logger.exception(f"[知识库] 文档转换失败 doc={doc.id}: {e}")
                    doc.status = "failed"
                    doc.error = str(e)
                    db.commit()
                    if progress:
                        progress.report("失败", 100, str(e), status="failed", error=str(e))
            else:
                # Existing parser pipeline for text files
                try:
                    doc.status = "parsing"
                    db.commit()
                    if progress:
                        progress.update("解析中", 10, "抽取文本", status="parsing")
                    parser = _get_parser(doc.filename)
                    with open(path, "rb") as f:
                        raw_bytes = f.read()
                    # NUL 清理：PostgreSQL 拒绝含 0x00 的文本（SQLite 容忍，本地不会暴露）
                    sections = await parser.parse(raw_bytes.replace(b"\x00", b""), doc.filename)

                    from agentscope.rag import ApproxTokenChunker
                    chunker = ApproxTokenChunker(chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
                    chunks = await chunker.chunk(sections)
                    chunks = [c for c in chunks if _chunk_text(c).strip()]
                    if not chunks:
                        raise ValueError("文档未解析出任何有效内容")
                    if progress:
                        progress.update("解析完成", 45, f"共 {len(chunks)} 个切片")

                    doc.status = "indexing"
                    db.commit()
                    if progress:
                        progress.update("向量化", 65, "生成 embeddings", status="indexing")
                    # Report per-batch embedding progress so the bar advances
                    # smoothly instead of jumping 65% -> 100%.
                    total_chunks = len(chunks)
                    vectors = []
                    for i in range(0, total_chunks, EMBED_BATCH):
                        batch = chunks[i:i + EMBED_BATCH]
                        vecs = await embed_texts([_chunk_text(c) for c in batch])
                        vectors.extend(vecs)
                        if progress and total_chunks > 1:
                            pct = 65 + int(25 * (i + len(batch)) / total_chunks)
                            progress.report("向量化", min(pct, 90), f"已向量化 {i + len(batch)}/{total_chunks} 切片")

                    from agentscope.rag import VectorRecord
                    store = get_vector_store()
                    coll = _collection(doc.kb_id)
                    if not await store.has_collection(coll):
                        await store.create_collection(coll, EMBED_DIMENSIONS)
                    records = [
                        VectorRecord(vector=vec, document_id=str(doc.id), chunk=c)
                        for c, vec in zip(chunks, vectors)
                    ]
                    await store.replace_document(coll, str(doc.id), records)

                    doc.status = "ready"
                    doc.chunk_count = len(chunks)
                    doc.error = None
                    db.commit()
                    if progress:
                        progress.update("已完成", 100, f"共 {len(chunks)} 个切片", status="ready")
                    logger.info(f"[知识库] 文档入库完成 doc={doc.id} chunks={len(chunks)}")
                except KnowledgeImportCancelled:
                    doc.status = "canceled"
                    doc.error = None
                    db.commit()
                    if progress:
                        progress.report("已取消", 100, "用户取消了本次处理", status="canceled")
                except Exception as e:
                    logger.exception(f"[知识库] 文档入库失败 doc={doc.id}: {e}")
                    doc.status = "failed"
                    doc.error = str(e)
                    db.commit()
                    if progress:
                        progress.report("失败", 100, str(e), status="failed", error=str(e))
        except KnowledgeImportCancelled:
            doc.status = "canceled"
            doc.error = None
            db.commit()
            if progress:
                progress.report("已取消", 100, "用户取消了本次处理", status="canceled")
        finally:
            try:
                if _is_temp and path and os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
            db.close()
    async def ingest_vault_md(
        self,
        doc_id: int,
        progress: Optional[ProgressReporter] = None,
    ):
        """vault md 笔记向量化：读 vault_dir/vault_path，切块→embed→入库（复用管线）。独立 DB 会话。"""
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
            if not doc or not doc.is_markdown:
                return
            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
            if not kb or not kb.vault_dir:
                return
            path = os.path.join(kb.vault_dir, doc.vault_path or doc.filename)
            try:
                doc.status = "indexing"
                db.commit()
                if progress:
                    progress.update("向量化", 10, "读取笔记内容", status="indexing")
                from agentscope.rag import Chunk, VectorRecord
                from agentscope.message import TextBlock
                from app.services.knowledge.sentence_chunker import chunk_text
                # 源文档转换的笔记优先切"清洗后原文"（旁挂 .raw.txt，检索质量更高）；
                # 普通手写笔记没有旁挂文件，直接切 md 本身。
                raw_path = os.path.splitext(path)[0] + ".raw.txt"
                text_src = raw_path if os.path.isfile(raw_path) else path
                with open(text_src, "r", encoding="utf-8", errors="ignore") as f:
                    full_text = f.read()
                # NUL 清理：PostgreSQL 拒绝含 0x00 的文本（SQLite 容忍，本地不会暴露）
                full_text = full_text.replace("\x00", "")
                source_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
                chunk_strs = chunk_text(full_text)
                chunk_strs = [s for s in chunk_strs if s.strip()]
                if progress:
                    progress.update("向量化", 30, f"共 {len(chunk_strs)} 个切片")
                chunks = [
                    Chunk(content=TextBlock(text=s), source=doc.filename,
                          chunk_index=i, total_chunks=len(chunk_strs))
                    for i, s in enumerate(chunk_strs)
                ]
                coll = _collection(doc.kb_id)
                store = get_vector_store()
                if not await store.has_collection(coll):
                    await store.create_collection(coll, EMBED_DIMENSIONS)
                # 重新入库：先删该文档旧向量再插
                if not chunks:
                    raise ValueError("笔记未解析出可检索的正文")
                if progress:
                    progress.update("向量化", 55, "生成 embeddings")
                vectors = await embed_texts([_chunk_text(c) for c in chunks])
                records = [VectorRecord(vector=vec, document_id=str(doc.id), chunk=c)
                           for c, vec in zip(chunks, vectors)]
                if progress:
                    progress.update("向量化", 90, "写入向量库")
                await store.replace_document(coll, str(doc.id), records)
                doc.status = "ready"
                doc.chunk_count = len(chunks)
                doc.indexed_hash = source_hash
                doc.indexed_at = datetime.now()
                doc.error = None
                db.commit()
                if progress:
                    progress.report("已完成", 100, f"共 {len(chunks)} 个切片", status="ready")
                logger.info(f"[知识库] vault 笔记入库完成 doc={doc.id} chunks={len(chunks)}")
                # Auto-link by vector similarity
                try:
                    import numpy as np2
                    from app.models.knowledge import KnowledgeLink, KnowledgeDocument as KD2
                    store = get_vector_store()
                    coll = _collection(doc.kb_id)
                    doc_vecs = await asyncio.to_thread(store._doc_vectors, coll)
                    if str(doc.id) in doc_vecs:
                        new_vec = doc_vecs[str(doc.id)]
                        new_norm = np2.linalg.norm(new_vec) or 1.0
                        for did_str, vec in doc_vecs.items():
                            did = int(did_str)
                            if did == doc.id:
                                continue
                            norm = np2.linalg.norm(vec) or 1.0
                            sim = float(np2.dot(new_vec, vec) / (new_norm * norm))
                            if sim >= 0.85:
                                existing = db.query(KnowledgeLink).filter(
                                    KnowledgeLink.src_id == doc.id,
                                    KnowledgeLink.dst_id == did,
                                    KnowledgeLink.kind == "auto",
                                ).first()
                                if not existing:
                                    dst_doc = db.query(KD2).filter(KD2.id == did).first()
                                    dst_name = (dst_doc.title or dst_doc.filename) if dst_doc else str(did)
                                    db.add(KnowledgeLink(
                                        kb_id=doc.kb_id, src_id=doc.id, dst_id=did,
                                        dst_name=dst_name, kind="auto",
                                    ))
                                    # Bidirectional: also link existing doc -> new doc
                                    existing_rev = db.query(KnowledgeLink).filter(
                                        KnowledgeLink.src_id == did,
                                        KnowledgeLink.dst_id == doc.id,
                                        KnowledgeLink.kind == "auto",
                                    ).first()
                                    if not existing_rev:
                                        db.add(KnowledgeLink(
                                            kb_id=doc.kb_id, src_id=did, dst_id=doc.id,
                                            dst_name=doc.title or doc.filename, kind="auto",
                                        ))
                        db.commit()
                        logger.info(f"[知识库] 自动关联完成 doc={doc.id}")
                except Exception as e:
                    logger.error(f"[知识库] 自动关联失败 doc={doc.id}: {e}")

            except KnowledgeImportCancelled:
                doc.status = "canceled"
                doc.error = None
                db.commit()
                if progress:
                    progress.report("已取消", 100, "用户取消了本次处理", status="canceled")
            except Exception as e:
                logger.exception(f"[知识库] 文档处理失败 doc={doc.id}: {e}")
                doc.status = "failed"
                doc.error = str(e)
                db.commit()
                if progress:
                    progress.report("失败", 100, str(e), status="failed", error=str(e))
        except KnowledgeImportCancelled:
            doc.status = "canceled"
            doc.error = None
            db.commit()
            if progress:
                progress.report("已取消", 100, "用户取消了本次处理", status="canceled")
        finally:
            db.close()

    # ---- 检索 ----
    async def search(self, query: str, kb_ids: List[int], top_k: int = 5) -> List[dict]:
        """跨指定 KB 检索，返回 [{kb_id, document_id, filename, chunk_index, text, score}]"""
        if not query.strip() or not kb_ids:
            return []
        qvec = (await embed_texts([query.strip()]))[0]
        store = get_vector_store()
        results = []
        for kb_id in kb_ids:
            coll = _collection(kb_id)
            try:
                if not await store.has_collection(coll):
                    continue
                hits = await store.search(coll, qvec, top_k=top_k, query_text=query)
            except Exception as e:
                logger.error(f"[知识库] 检索失败 kb={kb_id}: {e}")
                continue
            for h in hits:
                results.append({
                    "kb_id": kb_id,
                    "document_id": h.document_id,
                    "filename": h.chunk.source,
                    "chunk_index": h.chunk.chunk_index,
                    "text": _chunk_text(h.chunk),
                    "score": round(float(h.score), 4),
                })
        if results:
            candidate_ids = {int(r["document_id"]) for r in results if str(r["document_id"]).isdigit()}
            valid_ids = {(doc.id, doc.kb_id) for doc in self.db.query(KnowledgeDocument).filter(
                KnowledgeDocument.id.in_(candidate_ids),
                KnowledgeDocument.kb_id.in_(kb_ids),
                KnowledgeDocument.status == "ready",
            ).all()}
            results = [r for r in results if str(r["document_id"]).isdigit()
                       and (int(r["document_id"]), r["kb_id"]) in valid_ids]
        # Link weighting (PageRank-like prior): boost by incoming link count
        try:
            from app.models.knowledge import KnowledgeLink
            from sqlalchemy import func
            doc_ids = set()
            for r in results:
                try:
                    doc_ids.add(int(r["document_id"]))
                except (ValueError, TypeError):
                    pass
            if doc_ids:
                link_counts = {str(doc_id): count for doc_id, count in
                               self.db.query(KnowledgeLink.dst_id, func.count(KnowledgeLink.id))
                               .filter(KnowledgeLink.dst_id.in_(doc_ids),
                                       KnowledgeLink.kb_id.in_(kb_ids))
                               .group_by(KnowledgeLink.dst_id).all()}
                for r in results:
                    key = str(r.get("document_id", ""))
                    if key in link_counts:
                        r["score"] = round(min(1.0, max(0.0, r["score"] *
                                             (1.0 + 0.03 * min(link_counts[key], 3)))), 4)
        except Exception:
            pass

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
