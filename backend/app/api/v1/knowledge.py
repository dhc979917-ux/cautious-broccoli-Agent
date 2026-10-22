"""知识库 API（多用户隔离 + Obsidian 模块）。

个人库（personal）：属主+管理员可见/管理；共享库（shared）：需 kb_manage 权限码。
vault_dir 非空 = Obsidian vault 型：md 进图谱+向量，Obsidian 可共编（L3 watcher）。
单文件 MAX_KNOWLEDGE_FILE_MB=0 不限制。"""

import logging

logger = logging.getLogger(__name__)

import json
import os, io, re, zipfile, uuid
from urllib.parse import quote
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.database import SessionLocal
from app.core.deps import get_current_user, has_perm, get_user_from_query_token
from app.core.security import decode_token
from app.models.user import User
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, KnowledgeLink
from app.services.knowledge_service import KnowledgeService, _kb_dir
from app.services.knowledge_service import _collection
from app.services.knowledge.markdown_indexer import MarkdownIndexer
from app.services.knowledge.graph_service import GraphService
from app.services.knowledge.classify_service import classify_suggest
from app.services.knowledge.progress import knowledge_upload_progress_registry
from app.services.knowledge.watcher import KnowledgeWatcher
from app.services.knowledge.index_jobs import enqueue
from app.services.vector_store import get_vector_store
from app.config import settings

router = APIRouter(prefix="/knowledge", tags=["知识库"])
progress_ws_router = APIRouter(tags=["知识库进度"])


class KBCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    scope: str = "personal"


class KBUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class LinkVaultRequest(BaseModel):
    vault_dir: str


class CategoryRequest(BaseModel):
    category: str


class CreateNoteRequest(BaseModel):
    name: str
    content: Optional[str] = None


class DocContentUpdate(BaseModel):
    content: str


class SearchRequest(BaseModel):
    query: str
    kb_ids: Optional[List[int]] = None
    top_k: int = 5


def _can_manage(kb: KnowledgeBase, user: User, perm_code: str) -> bool:
    if kb.scope == "personal":
        return kb.created_by == user.id  # 个人库仅属主管理，管理员也不介入
    if user.role == "admin":
        return True
    if kb.scope == "shared" and has_perm(user, perm_code):
        return True
    return False


def _can_view(kb: KnowledgeBase, user: User) -> bool:
    if kb.scope == "personal":
        return kb.created_by == user.id  # 个人库完全隔离，管理员也不可见
    if user.role == "admin":
        return True
    if kb.scope == "shared":
        return True
    return kb.created_by == user.id


def _kb_dict(kb: KnowledgeBase, user: User, owner_name: Optional[str] = None,
             doc_count: int = 0, ready_count: int = 0,
             failed_count: int = 0, processing_count: int = 0) -> dict:
    return {
        "id": kb.id, "name": kb.name, "description": kb.description,
        "embedding_model": kb.embedding_model or settings.EMBEDDING_MODEL,
        "scope": kb.scope, "kind": kb.kind, "vault_dir": kb.vault_dir,
        "owner_id": kb.created_by, "owner_name": owner_name,
        "can_manage": _can_manage(kb, user, "kb_manage.kb_create"),
        "created_at": kb.created_at.isoformat() if kb.created_at else None,
        "doc_count": doc_count, "ready_count": ready_count,
        "failed_count": failed_count, "processing_count": processing_count,
    }


# 处理中状态集合（与前端轮询口径一致）
PROCESSING_STATUSES = ("uploading", "parsing", "converting", "indexing")


def _kb_counts(svc: "KnowledgeService", kb_id: int) -> tuple:
    """库文档统计：(总数, 就绪, 失败, 处理中)。
    总数排除 converted（原件已转 md，其产物笔记另行计数）与 deleted（软删除）。"""
    docs = svc.list_documents(kb_id)
    doc_count = sum(1 for d in docs if d.status not in ("converted", "deleted", "archived"))
    ready = sum(1 for d in docs if d.status == "ready")
    failed = sum(1 for d in docs if d.status == "failed")
    processing = sum(1 for d in docs if d.status in PROCESSING_STATUSES)
    return doc_count, ready, failed, processing


def _doc_dict(doc: KnowledgeDocument) -> dict:
    return {
        "id": doc.id, "kb_id": doc.kb_id, "filename": doc.filename, "size": doc.size,
        "status": doc.status, "upload_batch_id": doc.upload_batch_id,
        "chunk_count": doc.chunk_count, "error": doc.error,
        "title": doc.title, "vault_path": doc.vault_path,
        "tags": _split_json(doc.tags_json), "category": doc.category,
        "is_markdown": doc.is_markdown,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _split_json(s):
    try:
        import json
        return json.loads(s or "[]")
    except Exception:
        return []




def _fix_filename_encoding(text: str) -> str:
    """Fix UTF-8 filename that was incorrectly decoded as GBK (common on Windows)."""
    try:
        return text.encode('gbk').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text

def _get_kb_or_404(db: Session, kb_id: int) -> KnowledgeBase:
    kb = KnowledgeService(db).get_kb(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _require_manage(kb: KnowledgeBase, user: User, perm_code: str):
    if not _can_manage(kb, user, perm_code):
        raise HTTPException(status_code=403, detail="无此知识库的管理权限")


def _vault_path_safe(kb: KnowledgeBase, rel: str) -> str:
    """防止路径越界：vault 内相对路径解析后必须仍在 vault_dir 内。"""
    base = os.path.abspath(kb.vault_dir)
    full = os.path.abspath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) and full != base:
        raise HTTPException(status_code=400, detail="非法路径")
    return full


# ===== 知识库 CRUD =====
@router.get("/bases")
async def list_kbs(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    kbs = svc.list_kbs_visible(current_user.id, current_user.role)
    owner_ids = {kb.created_by for kb in kbs if kb.created_by is not None}
    owners = {}
    if owner_ids:
        for u in db.query(User).filter(User.id.in_(owner_ids)).all():
            owners[u.id] = u.username
    result = []
    for kb in kbs:
        docs = svc.list_documents(kb.id)
        ready = sum(1 for d in docs if d.status == "ready")
        doc_count = sum(1 for d in docs if d.status != "converted")
        result.append(_kb_dict(kb, current_user, owners.get(kb.created_by), doc_count, ready))
    return result


@router.post("/bases")
async def create_kb(data: KBCreateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="名称不能为空")
    scope = data.scope if data.scope in ("personal", "shared") else "personal"
    if scope == "shared" and not has_perm(current_user, "kb_manage.kb_create"):
        raise HTTPException(status_code=403, detail="创建共享知识库需要权限")
    if db.query(KnowledgeBase).filter(KnowledgeBase.name == data.name.strip()).first():
        raise HTTPException(status_code=400, detail="同名知识库已存在")
    kb = KnowledgeService(db).create_kb(current_user.id, data.name.strip(), data.description, scope)
    return _kb_dict(kb, current_user, current_user.username)


@router.patch("/bases/{kb_id}")
async def update_kb(kb_id: int, data: KBUpdateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.kb_update")
    try:
        kb = KnowledgeService(db).update_kb(kb_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _kb_dict(kb, current_user)


@router.delete("/bases/{kb_id}")
async def delete_kb(kb_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.kb_delete")
    KnowledgeWatcher().stop_watch(kb_id)
    KnowledgeService(db).delete_kb(kb_id)
    return {"message": "已删除"}


# ===== 文档管理 =====
@router.get("/bases/{kb_id}/documents")
async def list_documents(kb_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return [_doc_dict(d) for d in KnowledgeService(db).list_documents(kb_id)]


@router.get("/bases/{kb_id}/archives")
async def memory_archives(kb_id: int, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    if kb.kind != "memory" or kb.scope != "personal" or kb.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该记忆归档")
    from app.agents.memory_archive import list_archives
    return list_archives(db, kb_id, current_user.id)


@router.post("/archives/{archive_id}/restore")
async def restore_memory_archive(archive_id: int, current_user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    from app.agents.memory_archive import restore_archive
    if not await restore_archive(db, archive_id, current_user.id):
        raise HTTPException(status_code=409, detail="归档已过期、文件不完整或原位置已有文件")
    return {"ok": True}


@router.post("/bases/{kb_id}/documents")
async def upload_documents(kb_id: int, background_tasks: BackgroundTasks, files: List[UploadFile] = File(...),
                            current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.doc_upload")
    max_bytes = settings.MAX_KNOWLEDGE_FILE_MB * 1024 * 1024
    svc = KnowledgeService(db)
    batch_id = uuid.uuid4().hex
    uploaded = []
    for f in files:
        content = await f.read()
        filename = _fix_filename_encoding(f.filename or "unnamed")
        if max_bytes and len(content) > max_bytes:
            raise HTTPException(status_code=400, detail=f"{filename} 超过 {settings.MAX_KNOWLEDGE_FILE_MB}MB 限制")
        stored_name = f"{uuid.uuid4().hex}_{filename}"
        # 写入本地磁盘：knowledge/{kb_id}/{stored_name}
        try:
            from app.services.knowledge_service import _kb_dir
            with open(os.path.join(_kb_dir(kb_id), stored_name), "wb") as fh:
                fh.write(content)
        except OSError as e:
            logger.error(f"[knowledge] 文件写入失败 {filename}: {e}")
            raise HTTPException(status_code=502, detail="文件存储失败")
        doc = svc.register_document(
            kb_id,
            current_user.id,
            filename,
            stored_name,
            len(content),
            upload_batch_id=batch_id,
        )
        db.commit()
        uploaded.append(_doc_dict(doc))
    tracker = knowledge_upload_progress_registry.get_or_create(batch_id, uploaded)
    for doc in uploaded:
        enqueue(db, kb_id, doc["id"])
    return uploaded


def _batch_documents(db: Session, batch_id: str) -> list[KnowledgeDocument]:
    return (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.upload_batch_id == batch_id)
        .order_by(KnowledgeDocument.id.asc())
        .all()
    )


def _require_batch_manage(db: Session, batch_id: str, current_user: User) -> list[KnowledgeDocument]:
    docs = _batch_documents(db, batch_id)
    if not docs:
        raise HTTPException(status_code=404, detail="上传批次不存在")
    for kb_id in {doc.kb_id for doc in docs}:
        kb = _get_kb_or_404(db, kb_id)
        _require_manage(kb, current_user, "kb_manage.doc_upload")
    return docs


@router.get("/uploads/{batch_id}/progress")
async def upload_progress(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    docs = _batch_documents(db, batch_id)
    if not docs:
        raise HTTPException(status_code=404, detail="上传批次不存在")
    for kb_id in {doc.kb_id for doc in docs}:
        kb = _get_kb_or_404(db, kb_id)
        if not _can_view(kb, current_user):
            raise HTTPException(status_code=403, detail="无权访问该知识库")
    tracker = knowledge_upload_progress_registry.get_or_create(batch_id, docs)

    async def event_stream():
        terminal_statuses = {"ready", "converted", "failed", "canceled"}
        async for snapshot in tracker.subscribe(heartbeat_interval=15.0):
            if snapshot is None:
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if snapshot["overall"]["pending"] == 0 and all(
                item["status"] in terminal_statuses for item in snapshot["documents"]
            ):
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/uploads/{batch_id}/cancel")
async def cancel_upload(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    docs = _require_batch_manage(db, batch_id, current_user)
    tracker = knowledge_upload_progress_registry.get_or_create(batch_id, docs)
    tracker.request_cancel()
    return tracker.snapshot()


@progress_ws_router.websocket("/ws/knowledge-progress/{batch_id}")
async def websocket_knowledge_progress(
    websocket: WebSocket,
    batch_id: str,
    token: str,
):
    """Stream upload progress over WebSocket.

    SSE is buffered by some HTTP proxies, while this app already has a
    proven WebSocket path for chat streaming.
    """
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001, reason="无效的认证令牌")
        return

    user_id = int(payload.get("sub"))
    await websocket.accept()

    try:
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not user.is_active:
                await websocket.close(code=4001, reason="用户不存在或已被禁用")
                return
            if int(payload.get("tv") or 0) != int(user.token_version or 0):
                await websocket.close(code=4001, reason="登录状态已失效，请重新登录")
                return
            if user.must_change_password:
                await websocket.close(code=4003, reason="首次登录必须先修改密码")
                return

            docs = _batch_documents(db, batch_id)
            if not docs:
                await websocket.close(code=4004, reason="上传批次不存在")
                return
            for kb_id in {doc.kb_id for doc in docs}:
                kb = _get_kb_or_404(db, kb_id)
                if not _can_view(kb, user):
                    await websocket.close(code=4003, reason="无权访问该知识库")
                    return

            tracker = knowledge_upload_progress_registry.get_or_create(batch_id, docs)

        terminal_statuses = {"ready", "converted", "failed", "canceled"}
        async for snapshot in tracker.subscribe(heartbeat_interval=15.0):
            if snapshot is None:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json({"type": "progress", **snapshot})
            if snapshot["overall"]["pending"] == 0 and all(
                item["status"] in terminal_statuses for item in snapshot["documents"]
            ):
                break
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning(f"[knowledge] 进度 WebSocket 异常 batch={batch_id}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    _require_manage(kb, current_user, "kb_manage.doc_delete")
    svc.delete_document(doc_id)
    return {"message": "已删除"}


def _can_delete_doc(kb: KnowledgeBase, user: User) -> bool:
    """共享库仅管理员可删；个人库仅属主可删（管理员不可见个人库）。"""
    if kb.scope == "personal":
        return kb.created_by == user.id
    if user.role == "admin":
        return True
    return False


@router.post("/docs/{doc_id}/soft-delete")
async def soft_delete_doc(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not _can_delete_doc(kb, current_user):
        raise HTTPException(status_code=403, detail="无删除权限")
    svc.soft_delete_document(doc_id)
    return {"message": "已软删除"}


@router.post("/docs/{doc_id}/restore")
async def restore_doc(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not _can_delete_doc(kb, current_user):
        raise HTTPException(status_code=403, detail="无恢复权限")
    svc.restore_document(doc_id)
    return {"message": "已恢复"}

@router.delete("/docs/{doc_id}/hard")
async def hard_delete_doc(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not _can_delete_doc(kb, current_user):
        raise HTTPException(status_code=403, detail="无删除权限")
    svc.hard_delete_document(doc_id)
    return {"message": "已完全删除"}


@router.post("/docs/{doc_id}/retry")
async def retry_doc(doc_id: int, background_tasks: BackgroundTasks,
                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not _can_delete_doc(kb, current_user):
        raise HTTPException(status_code=403, detail="无操作权限")
    if doc.status not in ("failed",):
        raise HTTPException(status_code=400, detail="仅失败文档可重试")
    batch_id = doc.upload_batch_id or uuid.uuid4().hex
    doc.upload_batch_id = batch_id
    # Clean up conversion residue before re-processing.
    # md 笔记行本身不做清理（清了会删掉它自己的 vault 笔记文件）
    if kb.vault_dir and not doc.is_markdown:
        try:
            base = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(doc.filename)[0]).strip()
            import frontmatter
            for fn in os.listdir(kb.vault_dir):
                if not fn.endswith(".md") or not (fn == base + ".md" or re.fullmatch(re.escape(base) + r"_\d+\.md", fn)):
                    continue
                md_path = os.path.join(kb.vault_dir, fn)
                if frontmatter.load(md_path).get("source_file") != doc.stored_name:
                    continue
                os.remove(md_path)
                raw_path = os.path.splitext(md_path)[0] + ".raw.txt"
                if os.path.isfile(raw_path):
                    os.remove(raw_path)
        except Exception as e:
            logger.error(f"[knowledge] retry cleanup error: {e}")
    doc.status = "parsing"
    doc.error = None
    db.commit()
    tracker = knowledge_upload_progress_registry.get_or_create(batch_id, [doc])
    tracker.retry_document(doc)
    enqueue(db, kb.id, doc_id)
    return {"message": "已重新加入处理队列", "doc_id": doc_id, "batch_id": batch_id}





def _detect_media_type(filename: str) -> str:
    """根据扩展名返回可在浏览器内预览的 media type；未知则返回 octet-stream。"""
    ext = os.path.splitext(filename)[1].lower()
    types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".html": "text/html",
    }
    return types.get(ext, "application/octet-stream")



@router.get("/files/{kb_id}/{filename}")
async def get_source_file(kb_id: int, filename: str,
                          current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Serve original uploaded file (PDF etc.): 本地磁盘 FileResponse。"""
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    file_path = os.path.join(_kb_dir(kb_id), filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, filename=filename)


_VAULT_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


@router.get("/vault-files/{kb_id}")
async def get_vault_file(kb_id: int, path: str = Query(...), token: str = Query(...),
                         db: Session = Depends(get_db)):
    """Serve vault 内嵌图片（media/）。

    笔记 md 里的 ![图像](media/x.png) 由 <img> 标签加载，浏览器无法带
    Authorization 头，因此走 ?token= 查询参数鉴权（与文件预览同模式）。"""
    user = await get_user_from_query_token(token, db)
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    rel = (path or "").replace("\\", "/").lstrip("/")
    if not rel.startswith("media/"):
        raise HTTPException(status_code=400, detail="仅允许访问 media/ 内的图片资源")
    full = _vault_path_safe(kb, rel)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    if os.path.splitext(full)[1].lower() not in _VAULT_IMG_EXTS:
        raise HTTPException(status_code=400, detail="仅支持图片资源")
    return FileResponse(full)

# ===== Obsidian vault =====
@router.post("/bases/{kb_id}/link-vault")
async def link_vault(kb_id: int, data: LinkVaultRequest, background_tasks: BackgroundTasks,
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """L3：把 KB 绑到一个 markdown 文件夹，建图谱 + 启动 watcher。"""
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.kb_update")
    vdir = os.path.abspath(data.vault_dir)
    if not os.path.isdir(vdir):
        raise HTTPException(status_code=400, detail="目录不存在")
    kb.vault_dir = vdir
    db.commit()
    stats = MarkdownIndexer(db).index_vault(kb)
    # 变更笔记后台向量化
    svc = KnowledgeService(db)
    for did in stats["changed"]:
        enqueue(db, kb_id, did)
    # 启动 watcher（watchdog 未装则跳过，降级为手动 reindex）
    watched = KnowledgeWatcher().start_watch(kb_id, vdir)
    return {"vault_dir": vdir, "watcher": watched, **stats}


@router.post("/bases/{kb_id}/import-vault")
async def import_vault(kb_id: int, background_tasks: BackgroundTasks,
                       file: UploadFile = File(...),
                       current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """L1：上传 vault zip，解压到 per-KB vault 目录，建图谱。"""
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.doc_upload")
    vdir = os.path.join(_kb_dir(kb_id), "vault")
    os.makedirs(vdir, exist_ok=True)
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        zf.extractall(vdir)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解压失败：{e}")
    kb.vault_dir = vdir
    db.commit()
    stats = MarkdownIndexer(db).index_vault(kb)
    svc = KnowledgeService(db)
    for did in stats["changed"]:
        enqueue(db, kb_id, did)
    KnowledgeWatcher().start_watch(kb_id, vdir)
    return {"vault_dir": vdir, **stats}


@router.post("/bases/{kb_id}/reindex")
async def reindex_vault(kb_id: int, background_tasks: BackgroundTasks,
                        current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """强制全量重索引图谱 + 重向量变化更笔记。"""
    kb = _get_kb_or_404(db, kb_id)
    _require_manage(kb, current_user, "kb_manage.kb_update")
    if not kb.vault_dir:
        raise HTTPException(status_code=400, detail="该库未绑定 vault 目录")
    stats = MarkdownIndexer(db).index_vault(kb)
    svc = KnowledgeService(db)
    for did in stats["changed"]:
        enqueue(db, kb_id, did)
    return stats


@router.get("/bases/{kb_id}/graph")
async def get_graph(kb_id: int, tags: Optional[str] = Query(None), folder: Optional[str] = Query(None),
                    depth: Optional[int] = Query(None), focus: Optional[int] = Query(None),
                    include_orphans: bool = Query(True), include_tags: bool = Query(True),
                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    tag_list = [t for t in (tags or "").split(",") if t] if tags else None
    return GraphService(db).graph(kb, tags=tag_list, folder=folder, depth=depth,
                                  focus=focus, include_orphans=include_orphans,
                                  include_tags=include_tags)


@router.get("/bases/{kb_id}/tree")
async def get_tree(kb_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return GraphService(db).tree(kb)


@router.get("/bases/{kb_id}/tags")
async def get_tags(kb_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    kb = _get_kb_or_404(db, kb_id)
    if not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return GraphService(db).tags(kb)


# ===== 单文档 =====
@router.get("/docs/{doc_id}")
async def get_doc(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问")
    content = None
    if doc.is_markdown and kb.vault_dir and doc.vault_path:
        try:
            with open(_vault_path_safe(kb, doc.vault_path), "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            content = f"读取失败：{e}"
    return {**_doc_dict(doc), "content": content}


@router.get("/docs/{doc_id}/backlinks")
async def get_backlinks(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问")
    return GraphService(db).backlinks(doc_id)


@router.get("/docs/{doc_id}/chunk/{chunk_index}")
async def get_chunk(doc_id: int, chunk_index: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc or doc.status != "ready":
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问")
    coll = _collection(doc.kb_id)
    store = get_vector_store()
    total, chunks = await store.list_chunks(coll, str(doc_id), limit=1, offset=chunk_index)
    if not chunks:
        raise HTTPException(status_code=404, detail="该文档无可用片段")
    if chunks[0]["chunk_index"] != chunk_index:
        raise HTTPException(status_code=404, detail="片段编号不匹配")
    return {
        "doc_id": doc_id,
        "filename": doc.filename,
        "title": doc.title,
        "is_markdown": doc.is_markdown,
        "chunk_index": chunks[0]["chunk_index"],
        "content": chunks[0]["content"],
        "total_chunks": total,
    }


@router.get("/docs/{doc_id}/raw")
async def get_raw_text(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc or doc.status != "ready":
        raise HTTPException(status_code=404, detail="doc not found")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="no access")
    coll = _collection(doc.kb_id)
    store = get_vector_store()
    total, chunks = await store.list_chunks(coll, str(doc_id), limit=999, offset=0)
    text = "\n\n---\n\n".join(c["content"] for c in chunks)
    return {"doc_id": doc_id, "filename": doc.filename, "content": text, "total_chunks": total}

@router.post("/docs/{doc_id}/classify-suggest")
async def doc_classify_suggest(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not _can_view(kb, current_user):
        raise HTTPException(status_code=403, detail="无权访问")
    existing = {d.category for d in svc.list_documents(kb.id) if d.category}
    text = ""
    if doc.is_markdown and kb.vault_dir and doc.vault_path:
        try:
            text = open(_vault_path_safe(kb, doc.vault_path), "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            text = ""
    return classify_suggest(doc.title or doc.filename, text, list(existing))


@router.post("/docs/{doc_id}/category")
async def set_doc_category(doc_id: int, data: CategoryRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    _require_manage(kb, current_user, "kb_manage.kb_update")
    doc.category = data.category.strip() or None
    db.commit()
    return {"category": doc.category}


@router.post("/docs/{doc_id}/create")
async def create_ghost_note(doc_id: int, data: CreateNoteRequest, background_tasks: BackgroundTasks,
                             current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """P2：把 ghost 节点（dst_name）创建为真实 .md（在调用方所在 vault 内）。"""
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not kb.vault_dir:
        raise HTTPException(status_code=400, detail="需先绑定 vault 目录")
    _require_manage(kb, current_user, "kb_manage.doc_upload")
    name = "".join(c for c in data.name if c not in '\\/:*?"<>|').strip() or "未命名"
    if not name.lower().endswith(".md"):
        name += ".md"
    full = _vault_path_safe(kb, name)
    if os.path.exists(full):
        raise HTTPException(status_code=400, detail="笔记已存在")
    body = data.content or f"# {os.path.splitext(name)[0]}\n"
    with open(full, "w", encoding="utf-8") as f:
        f.write(body)
    MarkdownIndexer(db).index_file(kb, full)
    new_doc = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.vault_path == name
    ).first()
    if new_doc:
        enqueue(db, kb.id, new_doc.id)
    return {"vault_path": name, "doc_id": new_doc.id if new_doc else None}


@router.post("/docs/{doc_id}/write-frontmatter")
async def write_frontmatter(doc_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """P2 opt-in：把 DB 里的 category/tags 回写进 md frontmatter。"""
    import frontmatter
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not kb.vault_dir:
        raise HTTPException(status_code=400, detail="需先绑定 vault 目录")
    _require_manage(kb, current_user, "kb_manage.kb_update")
    full = _vault_path_safe(kb, doc.vault_path)
    try:
        post = frontmatter.load(full)
        if doc.category:
            post["category"] = doc.category
        if doc.tags_json:
            import json
            post["tags"] = json.loads(doc.tags_json)
        with open(full, "w", encoding="utf-8") as f:
            frontmatter.dump(post, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"回写失败：{e}")
    MarkdownIndexer(db).index_file(kb, full)
    return {"ok": True}


@router.put("/docs/{doc_id}/content")
async def update_doc_content(doc_id: int, data: DocContentUpdate,
                             background_tasks: BackgroundTasks,
                             current_user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    """Edit MD content. Personal KB: owner or admin. Shared KB: admin only."""
    svc = KnowledgeService(db)
    doc = svc.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not doc.is_markdown:
        raise HTTPException(status_code=400, detail="非 Markdown 文档不可编辑")
    kb = svc.get_kb(doc.kb_id)
    if not kb or not kb.vault_dir:
        raise HTTPException(status_code=400, detail="需先绑定 vault 目录")
    # Permission: personal -> owner or admin; shared -> admin only
    if kb.scope == "shared":
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可编辑共享知识库")
    else:
        _require_manage(kb, current_user, "kb_manage.kb_update")
    full = _vault_path_safe(kb, doc.vault_path or doc.filename)
    with open(full, "w", encoding="utf-8") as f:
        f.write(data.content)
    # Re-index the MD file (updates links, frontmatter, etc.)
    MarkdownIndexer(db).index_file(kb, full)
    # Re-vectorize in background
    new_doc = db.query(KnowledgeDocument).filter(
        KnowledgeDocument.id == doc_id
    ).first()
    if new_doc:
        enqueue(db, kb.id, new_doc.id)
    # Return full doc data (title/tags updated by indexer) so frontend refreshes in one round-trip
    content = None
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        pass
    return {**_doc_dict(new_doc), "content": content}


# ===== 检索（按可见性过滤）=====
@router.post("/search")
async def search(data: SearchRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    svc = KnowledgeService(db)
    visible = svc.list_kbs_visible(current_user.id, current_user.role)
    visible_ids = {kb.id for kb in visible}
    if data.kb_ids is not None:
        kb_ids = [i for i in data.kb_ids if i in visible_ids]
    else:
        kb_ids = list(visible_ids)
    top_k = max(1, min(int(data.top_k or 5), 20))
    return await svc.search(data.query, kb_ids, top_k=top_k)
