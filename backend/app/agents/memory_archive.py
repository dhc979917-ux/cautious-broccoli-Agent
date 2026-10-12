"""Recoverable, owner-scoped seven-day storage for absorbed memory sources."""

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, MemoryArchive
from app.services.knowledge.index_jobs import enqueue

RETENTION_DAYS = 7
ARCHIVE_DIR = ".memory_archive"


def _safe_path(vault: str, relative: str) -> str:
    base = os.path.realpath(vault)
    full = os.path.realpath(os.path.join(base, relative))
    if not full.startswith(base + os.sep):
        raise ValueError("memory path outside vault")
    return full


def _digest_indexed(vault: str, digest_rel: str) -> bool:
    if not digest_rel:
        return True
    db = SessionLocal()
    try:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.vault_dir == vault,
            KnowledgeBase.kind == "memory",
        ).first()
        if kb is None or not os.path.isfile(_safe_path(vault, digest_rel)):
            return False
        with open(_safe_path(vault, digest_rel), "rb") as handle:
            content = handle.read()
        doc = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id,
            KnowledgeDocument.vault_path == digest_rel,
            KnowledgeDocument.status == "ready",
            KnowledgeDocument.chunk_count > 0,
            KnowledgeDocument.content_hash == hashlib.md5(content).hexdigest(),
            KnowledgeDocument.indexed_hash == hashlib.sha256(
                content.decode("utf-8", errors="ignore").replace("\x00", "").encode("utf-8")
            ).hexdigest(),
        ).first()
        return doc is not None
    finally:
        db.close()


def _ensure_digest_queued(vault: str, digest_rel: str) -> bool:
    from app.services.knowledge.markdown_indexer import MarkdownIndexer

    db = SessionLocal()
    try:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.vault_dir == vault, KnowledgeBase.kind == "memory"
        ).first()
        if kb is None:
            return False
        full = _safe_path(vault, digest_rel)
        if not os.path.isfile(full):
            return False
        doc_id = MarkdownIndexer(db).index_file(kb, full, commit=False)
        if not doc_id:
            return False
        enqueue(db, kb.id, doc_id, commit=False)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def archive_source(app, vault: str, source_rel: str, digest_rel: str = "",
                         *, expected_hash: str | None = None) -> bool:
    from app.agents.memory import sync_memory_index

    vault = os.path.realpath(vault)
    source = _safe_path(vault, source_rel)
    if not os.path.isfile(source):
        return False
    if digest_rel:
        if not await asyncio.to_thread(_digest_indexed, vault, digest_rel):
            try:
                if not await asyncio.to_thread(_ensure_digest_queued, vault, digest_rel):
                    return False
            except Exception:
                return False
        for _ in range(15):
            if await asyncio.to_thread(_digest_indexed, vault, digest_rel):
                break
            await asyncio.sleep(1)
        else:
            return False
    db = SessionLocal()
    try:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.vault_dir == vault,
            KnowledgeBase.kind == "memory",
        ).first()
        if kb is None or kb.created_by is None:
            return False
        archive_rel = os.path.join(ARCHIVE_DIR, uuid.uuid4().hex + ".bak")
        target = _safe_path(vault, archive_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(source, "rb") as handle:
            checksum = hashlib.sha256(handle.read()).hexdigest()
        if expected_hash is not None and checksum != expected_hash:
            return False
        os.replace(source, target)
        try:
            with open(target, "rb") as handle:
                if hashlib.sha256(handle.read()).hexdigest() != checksum:
                    raise RuntimeError("source changed during archive")
            if source_rel.endswith(".md") and not await sync_memory_index(
                app, vault, [source_rel], who="memory-archive"
            ):
                raise RuntimeError("ReMe index removal failed")
            db.add(MemoryArchive(
                kb_id=kb.id, user_id=kb.created_by, source_rel=source_rel,
                archive_rel=archive_rel, digest_rel=digest_rel,
                source_hash=checksum, absorbed_at=datetime.now(),
                expires_at=datetime.now() + timedelta(days=RETENTION_DAYS),
            ))
            doc = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb.id,
                KnowledgeDocument.vault_path == source_rel,
            ).first()
            if doc is not None:
                doc.status = "archived"
                enqueue(db, kb.id, doc.id, "delete", commit=False)
            db.commit()
            return True
        except Exception:
            db.rollback()
            os.replace(target, source)
            if source_rel.endswith(".md"):
                await sync_memory_index(app, vault, [source_rel], who="memory-archive-rollback")
            return False
    finally:
        db.close()


def _has_live_digest(db, kb_id: int, vault: str, digest_rel: str) -> bool:
    for _ in range(8):
        if not digest_rel:
            return True
        if _digest_indexed(vault, digest_rel):
            return True
        parent = db.query(MemoryArchive).filter(
            MemoryArchive.kb_id == kb_id,
            MemoryArchive.source_rel == digest_rel,
            MemoryArchive.state.in_(("archived", "expired")),
        ).order_by(MemoryArchive.id.desc()).first()
        if parent is None:
            return False
        digest_rel = parent.digest_rel
    return False


def purge_expired(vault: str) -> int:
    db = SessionLocal()
    removed = 0
    try:
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.vault_dir == os.path.realpath(vault),
            KnowledgeBase.kind == "memory",
        ).first()
        if kb is None:
            return 0
        rows = db.query(MemoryArchive).filter(
            MemoryArchive.kb_id == kb.id,
            MemoryArchive.state == "archived",
            MemoryArchive.expires_at <= datetime.now(),
        ).all()
        for row in rows:
            if not _has_live_digest(db, kb.id, vault, row.digest_rel):
                continue
            path = _safe_path(vault, row.archive_rel)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != row.source_hash:
                        continue
                os.remove(path)
            row.state = "expired"
            removed += 1
        db.commit()
        return removed
    finally:
        db.close()


def list_archives(db, kb_id: int, user_id: int) -> list[dict]:
    rows = db.query(MemoryArchive).filter(
        MemoryArchive.kb_id == kb_id,
        MemoryArchive.user_id == user_id,
        MemoryArchive.state == "archived",
        MemoryArchive.expires_at > datetime.now(),
    ).order_by(MemoryArchive.id.desc()).all()
    return [dict(id=row.id, source_rel=row.source_rel,
                 absorbed_at=row.absorbed_at.isoformat(),
                 expires_at=row.expires_at.isoformat()) for row in rows]


async def restore_archive(db, archive_id: int, user_id: int) -> bool:
    row = db.query(MemoryArchive).filter(
        MemoryArchive.id == archive_id,
        MemoryArchive.user_id == user_id,
        MemoryArchive.state == "archived",
        MemoryArchive.expires_at > datetime.now(),
    ).first()
    if row is None:
        return False
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == row.kb_id,
        KnowledgeBase.created_by == user_id,
        KnowledgeBase.kind == "memory",
    ).first()
    if kb is None or not kb.vault_dir:
        return False
    source = _safe_path(kb.vault_dir, row.source_rel)
    archived = _safe_path(kb.vault_dir, row.archive_rel)
    if os.path.exists(source) or not os.path.isfile(archived):
        return False
    with open(archived, "rb") as handle:
        if hashlib.sha256(handle.read()).hexdigest() != row.source_hash:
            return False
    os.makedirs(os.path.dirname(source), exist_ok=True)
    os.replace(archived, source)
    try:
        if row.source_rel.endswith(".md"):
            from app.services.knowledge.markdown_indexer import MarkdownIndexer
            doc_id = MarkdownIndexer(db).index_file(kb, source, commit=False)
            if doc_id:
                doc = db.query(KnowledgeDocument).filter_by(id=doc_id).first()
                doc.status = "indexing"
                enqueue(db, kb.id, doc.id, commit=False)
        row.state = "restored"
        db.commit()
    except Exception:
        db.rollback()
        os.replace(source, archived)
        raise
    if row.source_rel.endswith(".md"):
        from app.agents.memory import get_memory_middleware, sync_memory_index
        middleware = get_memory_middleware(user_id)
        if middleware is not None:
            try:
                await middleware._ensure_started()
                await sync_memory_index(middleware._app, kb.vault_dir,
                                        [row.source_rel], who="memory-restore")
            except Exception:
                pass
    return True
