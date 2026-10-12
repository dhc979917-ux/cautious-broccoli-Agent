"""Durable, idempotent indexing requests for document and vault changes."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import inspect, or_, text

from app.database import SessionLocal, engine
from app.models.knowledge import KnowledgeDocument, KnowledgeIndexJob

logger = logging.getLogger(__name__)


def enqueue(db, kb_id: int, doc_id: int, action: str = "ingest", *, commit: bool = True) -> int:
    doc = db.query(KnowledgeDocument).filter_by(id=doc_id).first()
    revision = doc.content_hash if doc is not None and action == "ingest" else None
    pending = db.query(KnowledgeIndexJob).filter(
        KnowledgeIndexJob.doc_id == doc_id,
        KnowledgeIndexJob.kb_id == kb_id,
        KnowledgeIndexJob.action == action,
        KnowledgeIndexJob.status == "pending",
    ).first()
    if pending is not None:
        pending.revision = revision
        if commit:
            db.commit()
        else:
            db.flush()
        return pending.id
    job = KnowledgeIndexJob(
        kb_id=kb_id, doc_id=doc_id, action=action,
        revision=revision, status="pending",
    )
    db.add(job)
    if commit:
        db.commit()
    else:
        db.flush()
    return job.id


def _claim():
    db = SessionLocal()
    try:
        now = datetime.now()
        candidate = db.query(KnowledgeIndexJob).filter(or_(
            KnowledgeIndexJob.status == "pending",
            (KnowledgeIndexJob.status == "running") & (KnowledgeIndexJob.lease_until < now),
        )).order_by(KnowledgeIndexJob.id).with_for_update(skip_locked=True).first()
        if candidate is None:
            return None
        candidate.status = "running"
        candidate.attempts += 1
        candidate.lease_until = now + timedelta(minutes=15)
        db.commit()
        return candidate.id
    finally:
        db.close()


async def _run(job_id: int):
    from app.services.knowledge_service import KnowledgeService, _collection
    from app.services.vector_store import get_vector_store
    from app.services.knowledge.progress import knowledge_upload_progress_registry

    db = SessionLocal()
    try:
        job = db.query(KnowledgeIndexJob).filter_by(id=job_id).first()
        if job is None:
            return
        doc = db.query(KnowledgeDocument).filter_by(id=job.doc_id).first()
        if job.action == "drop":
            await get_vector_store().delete_collection(_collection(job.kb_id))
        elif job.action == "delete":
            await get_vector_store().delete(_collection(job.kb_id), str(job.doc_id))
        elif doc is None or doc.status in ("deleted", "archived", "canceled"):
            await get_vector_store().delete(_collection(job.kb_id), str(job.doc_id))
        else:
            reporter = None
            if doc.upload_batch_id:
                tracker = knowledge_upload_progress_registry.get_or_create(
                    doc.upload_batch_id,
                    db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.upload_batch_id == doc.upload_batch_id
                    ).all(),
                )
                try:
                    reporter = tracker.reporter(doc.id)
                except KeyError:
                    pass
            if doc.is_markdown:
                await KnowledgeService(db).ingest_vault_md(doc.id, reporter)
            else:
                await KnowledgeService(db).ingest_document(doc.id, reporter)
            db.expire_all()
            doc = db.query(KnowledgeDocument).filter_by(id=job.doc_id).first()
            if doc is not None and doc.status not in ("ready", "converted", "canceled"):
                raise RuntimeError(doc.error or f"index status: {doc.status}")
            if doc is not None and doc.is_markdown and job.revision != doc.content_hash:
                enqueue(db, doc.kb_id, doc.id)
        job.status = "done"
        job.error = None
        job.lease_until = None
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.query(KnowledgeIndexJob).filter_by(id=job_id).first()
        if job is not None:
            job.status = "failed" if job.attempts >= 3 else "pending"
            job.lease_until = None
            job.error = str(exc)[:1000]
            db.commit()
        logger.error("[knowledge-jobs] job=%s failed: %s", job_id, exc)
        await asyncio.sleep(min(2 ** (job.attempts if job else 1), 30))
    finally:
        db.close()


async def run_worker():
    while True:
        try:
            job_id = await asyncio.to_thread(_claim)
        except Exception:
            logger.exception("[knowledge-jobs] unable to claim job")
            await asyncio.sleep(5)
            continue
        if job_id is None:
            await asyncio.sleep(1)
            continue
        await _run(job_id)


def enqueue_repairs() -> dict:
    from app.models.knowledge import KnowledgeBase

    db = SessionLocal()
    counts = {"missing": 0, "orphan": 0}
    try:
        for doc in db.query(KnowledgeDocument).filter(
            KnowledgeDocument.is_markdown.is_(True),
            KnowledgeDocument.status == "ready",
            KnowledgeDocument.size > 0,
            KnowledgeDocument.chunk_count == 0,
        ).all():
            kb = db.query(KnowledgeBase).filter_by(id=doc.kb_id).first()
            if kb is not None and kb.vault_dir:
                enqueue(db, kb.id, doc.id)
                counts["missing"] += 1
        if engine.dialect.name == "postgresql" and inspect(engine).has_table("vector_chunks"):
            rows = db.execute(text(
                "SELECT DISTINCT v.collection, v.document_id FROM vector_chunks v "
                "LEFT JOIN knowledge_documents d ON d.id::text=v.document_id "
                "AND v.collection=('kb_' || d.kb_id::text) "
                "WHERE d.id IS NULL OR d.status IN ('archived', 'deleted')"
            )).fetchall()
            for collection, doc_id in rows:
                if collection.startswith("kb_") and collection[3:].isdigit() and doc_id.isdigit():
                    enqueue(db, int(collection[3:]), int(doc_id), "delete")
                    counts["orphan"] += 1
        return counts
    finally:
        db.close()
