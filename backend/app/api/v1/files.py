"""文件中心 API

权限模型：
  - 普通用户：只能查看/操作自己的文件（SQL 层强制 user_id=本人）
  - file_all.* 细粒度权限（仅作用于"他人文件/全局能力"，本人文件一律放行）：
      view            查看全部用户文件与全局统计
      download.single / download.batch   下载他人文件（单文件 / 批量）
      manage.rename / manage.soft_delete / manage.restore   管理他人文件
      hard_delete     彻底删除他人文件（凭据提供方也需该权限）

存储说明：文件内容存储在本地磁盘（uploads/ 与 outputs/user_{id}/），
按 DB 记录的 stored_name 读取。
"""

import logging

logger = logging.getLogger(__name__)

import os
import zipfile
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.core.deps import get_current_user, has_perm, has_module_perm
from app.core.security import verify_password
from app.models.user import User
from app.models.file import FileRecord
from app.schemas.file import FileResponse as FileSchema, FileRenameRequest, BatchDownloadRequest
from app.schemas.destroy import DestroyRequest
from app.services.file_service import FileService

router = APIRouter(prefix="/files", tags=["文件中心"])

# 批量下载保护上限
MAX_BATCH_FILES = 200


def _can_access(current_user: User, rec: FileRecord, op: str) -> bool:
    """本人文件一律放行；他人文件需对应 file_all.* 子权限。
    op 取值：download.single / download.batch / manage.rename /
             manage.soft_delete / manage.restore / hard_delete"""
    if rec.user_id == current_user.id:
        return True
    return has_perm(current_user, f"file_all.{op}")


def _get_owned_file(file_id: int, current_user: User, db: Session, op: str) -> FileRecord:
    """取文件并做归属/操作权限校验，不存在/无权分别 404/403"""
    rec = db.query(FileRecord).filter(FileRecord.id == file_id).first()
    if rec is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not _can_access(current_user, rec, op):
        raise HTTPException(status_code=403, detail="无权访问该文件")
    return rec


# ---------- 列表 / 统计 / 对账（固定路径必须放在 /{file_id} 之前） ----------

@router.get("/", response_model=list[FileSchema])
async def list_files(
    kind: Optional[str] = Query(None, description="upload / output"),
    q: Optional[str] = Query(None, description="按文件名模糊搜索"),
    session_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    deleted: bool = Query(False, description="True=查回收站"),
    user_id: Optional[int] = Query(None, description="仅管理员可用：按用户过滤；不传=全部"),
    mine: bool = Query(False, description="True=强制只看本人文件（管理员的'我的上传/分析产出'视图用）"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = FileService(db)
    if mine:
        # "我的"视图：无论是否管理员都只看本人，忽略 user_id
        rows = service.list_files(user_id=current_user.id, kind=kind, q=q,
                                  session_id=session_id, agent_id=agent_id, deleted=deleted)
    elif has_perm(current_user, "file_all.view"):
        # 有查看权限：可按用户过滤，不传则全量
        rows = service.list_files(user_id=user_id, kind=kind, q=q,
                                  session_id=session_id, agent_id=agent_id, deleted=deleted)
    else:
        # 普通用户：强制只看自己，忽略传入的 user_id
        rows = service.list_files(user_id=current_user.id, kind=kind, q=q,
                                  session_id=session_id, agent_id=agent_id, deleted=deleted)
    return rows


@router.get("/stats")
async def file_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的用量 + 配额；管理员额外返回全局统计（总量/总用量，不再做逐用户排行）"""
    service = FileService(db)
    result = {"my_usage": service.usage(current_user.id)}
    if has_perm(current_user, "file_all.view"):
        result["global"] = service.stats()
    return result


@router.post("/download-zip")
async def batch_download(
    data: BatchDownloadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量下载：把有权访问的文件打包成 zip 返回"""
    ids = data.file_ids[:MAX_BATCH_FILES]
    if not ids:
        raise HTTPException(status_code=400, detail="未选择文件")
    recs = db.query(FileRecord).filter(FileRecord.id.in_(ids)).all()
    allowed = [r for r in recs if _can_access(current_user, r, "download.batch")]
    if not allowed:
        raise HTTPException(status_code=403, detail="没有可下载的文件")

    tmp = tempfile.NamedTemporaryFile(prefix="files_", suffix=".zip", delete=False)
    tmp.close()
    used_names = set()
    service = FileService(db)
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for rec in allowed:
            # 内容从本地磁盘读取；缺失则跳过
            try:
                data = service.read_content(rec)
            except FileNotFoundError:
                continue
            # zip 内重名自动加序号
            arcname = rec.original_name
            n = 1
            while arcname in used_names:
                base, ext = os.path.splitext(rec.original_name)
                arcname = f"{base}({n}){ext}"
                n += 1
            used_names.add(arcname)
            zf.writestr(zipfile.ZipInfo(arcname), data)
    if has_module_perm(current_user, "file_all"):
        logger.info(f"[AUDIT] 文件权限用户 {current_user.username} 批量下载 {len(allowed)} 个文件")
    return FileResponse(
        tmp.name, media_type="application/zip", filename="files.zip",
        background=BackgroundTask(os.remove, tmp.name),
    )


# ---------- 单文件操作 ----------

@router.get("/{file_id}/download")
async def download_file(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = _get_owned_file(file_id, current_user, db, "download.single")
    if rec.user_id != current_user.id:
        logger.info(f"[AUDIT] {current_user.username} 下载用户 {rec.user_id} 的文件 {rec.stored_name}")
    try:
        data = FileService(db).read_content(rec)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="未找到该文件")
    return Response(
        content=data,
        media_type=rec.content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(rec.original_name)}"},
    )


@router.put("/{file_id}", response_model=FileSchema)
async def rename_file(
    file_id: int,
    data: FileRenameRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = _get_owned_file(file_id, current_user, db, "manage.rename")
    new_name = data.original_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    service = FileService(db)
    return service.rename(rec.id, new_name[:255])


@router.delete("/{file_id}")
async def soft_delete_file(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = _get_owned_file(file_id, current_user, db, "manage.soft_delete")
    FileService(db).soft_delete(rec.id)
    return {"message": "文件已移入回收站"}


@router.post("/{file_id}/restore")
async def restore_file(
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = _get_owned_file(file_id, current_user, db, "manage.restore")
    FileService(db).restore(rec.id)
    return {"message": "文件已恢复"}


@router.delete("/{file_id}/hard")
async def hard_delete_file(
    file_id: int,
    data: DestroyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """彻底删除（磁盘+记录），需要密码二次确认：
    删他人文件需 file_all.hard_delete 权限且凭据提供方也需该权限；删自己的文件需本人凭据。"""
    rec = _get_owned_file(file_id, current_user, db, "hard_delete")

    # 凭据按账号查找，兼容旧用户名
    cred_user = db.query(User).filter(
        (User.account == data.username) | (User.username == data.username)
    ).first()
    if not cred_user or not verify_password(data.password, cred_user.password_hash):
        raise HTTPException(status_code=403, detail="密码验证失败")
    if rec.user_id != current_user.id:
        if not has_perm(cred_user, "file_all.hard_delete"):
            raise HTTPException(status_code=403, detail="彻底删除他人文件需要提供拥有该权限的用户凭据")
    else:
        if cred_user.id != current_user.id:
            raise HTTPException(status_code=403, detail="只能使用本人凭据删除自己的文件")

    try:
        FileService(db).hard_delete(rec.id)
    except OSError as e:
        raise HTTPException(status_code=502, detail=f"文件删除失败，请重试（{e}）")
    return {"message": "文件已彻底删除，不可恢复"}
