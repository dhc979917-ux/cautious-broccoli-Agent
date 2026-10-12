"""
文件上传/下载 API

文件中心内容存储在本地磁盘（uploads/ 与 outputs/user_{id}/），上传直接
落盘；下载优先查 DB 记录按 stored_name 读本地磁盘，未登记的历史文件按
原路径直接回读。
"""

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session as DbSession

from app.config import UPLOAD_DIR, OUTPUT_DIR, settings
from app.database import get_db
from app.core.deps import get_current_user, get_user_from_query_token
from app.models.user import User
from app.models.file import FileRecord
from app.services.file_service import FileService
import os
import time
import mimetypes
import uuid
from urllib.parse import quote

router = APIRouter(prefix="/upload", tags=["文件上传"])

# 目录路径统一定义在 app.config（Path 对象），这里转字符串方便 os.path 操作
UPLOAD_DIR_STR = str(UPLOAD_DIR)
OUTPUT_DIR_STR = str(OUTPUT_DIR)

# 确保目录存在
os.makedirs(UPLOAD_DIR_STR, exist_ok=True)
os.makedirs(OUTPUT_DIR_STR, exist_ok=True)

# 最大文件大小: 50MB
MAX_FILE_SIZE = 50 * 1024 * 1024


def _safe_resolve(base_dir: str, filename: str):
    """把用户传入的文件名（支持嵌套相对路径）安全地解析到 base_dir 内，防路径穿越。

    成功返回绝对路径，失败（穿越企图）返回 None。
    """
    if not filename:
        return None
    # 禁止绝对路径、盘符；统一分隔符后逐段检查，禁止空段/. /..
    if os.path.isabs(filename) or ":" in filename or filename.startswith(("\\", "/")):
        return None
    parts = filename.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, *parts))
    if not path.startswith(base + os.sep):
        return None
    return path


def _guess_content_type(filename: str) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def _disposition(filename: str) -> dict:
    """RFC 5987 编码的下载头（兼容中文文件名）"""
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


@router.post("/")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """上传文件到服务器"""
    # 检查文件大小
    content = await file.read(MAX_FILE_SIZE + 1)
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件大小超过限制（最大50MB）")

    # 文件中心配额检查（每用户总量限制）
    file_service = FileService(db)
    if not file_service.check_quota(current_user.id, len(content)):
        raise HTTPException(
            status_code=413,
            detail=f"存储空间不足（每用户配额 {settings.FILE_QUOTA_MB}MB），请先在文件中心清理文件",
        )

    # 生成安全文件名: {user_id}_{timestamp}_{原文件名}
    # basename 去掉客户端可能带的路径，防止写入 uploads 目录之外
    original = os.path.basename(file.filename or "unnamed")
    safe_name = original.replace(" ", "_").replace("/", "_").replace("\\", "_")
    stored_name = f"{current_user.id}_{int(time.time())}_{uuid.uuid4().hex[:12]}_{safe_name}"

    # 写入本地磁盘：uploads/{stored_name}（stored_name 已含 user_id 前缀，无冲突）
    try:
        with open(os.path.join(UPLOAD_DIR_STR, stored_name), "wb") as f:
            f.write(content)
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"文件存储写入失败，请稍后重试（{e}）")

    # 登记文件元数据（文件中心列表/配额/管理员视图的数据源）
    file_service.register_upload(
        user_id=current_user.id,
        stored_name=stored_name,
        original_name=original,
        size=len(content),
        content_type=file.content_type,
    )

    # 返回文件信息（file_path 保留旧字段形状，兼容前端；聊天附件按 stored_name 解析）
    return {
        "stored_name": stored_name,
        "original_name": file.filename,
        "file_path": os.path.join(UPLOAD_DIR_STR, stored_name),
        "size": len(content),
        "content_type": file.content_type,
    }


# {filename:path} 允许嵌套相对路径（技能可能产出 projects/xxx/file 这类目录结构）
@router.get("/download/{filename:path}")
async def download_file(
    filename: str,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """下载输出目录中的文件（仅限当前用户自己的输出目录）"""
    # 优先按 DB 记录读本地磁盘（本人 output 记录才放行）
    rec = db.query(FileRecord).filter(
        FileRecord.user_id == current_user.id,
        FileRecord.kind == "output",
        FileRecord.stored_name == filename.replace("\\", "/"),
    ).first()
    if rec:
        try:
            data = FileService(db).read_content(rec)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在")
        return Response(
            content=data,
            media_type=_guess_content_type(filename),
            headers=_disposition(os.path.basename(filename)),
        )
    # 遗留回退：本地 outputs 目录（未登记的历史文件）
    user_output_dir = os.path.join(OUTPUT_DIR_STR, f"user_{current_user.id}")
    file_path = _safe_resolve(user_output_dir, filename)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=_guess_content_type(filename),
    )


@router.get("/preview/{filename:path}")
async def preview_file(
    filename: str,
    token: str,
    db: DbSession = Depends(get_db),
):
    """在线预览输出文件（inline disposition，浏览器直接渲染）。
    用 ?token=xxx 认证（新标签页无法发 Authorization header）。"""
    current_user = await get_user_from_query_token(token=token, db=db)
    rec = db.query(FileRecord).filter(
        FileRecord.user_id == current_user.id,
        FileRecord.kind == "output",
        FileRecord.stored_name == filename.replace("\\", "/"),
    ).first()
    if rec:
        try:
            data = FileService(db).read_content(rec)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在")
        return Response(
            content=data,
            media_type=_guess_content_type(filename),
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(os.path.basename(filename))}"},
        )
    user_output_dir = os.path.join(OUTPUT_DIR_STR, f"user_{current_user.id}")
    file_path = _safe_resolve(user_output_dir, filename)
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path=file_path,
        media_type=_guess_content_type(filename),
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(os.path.basename(filename))}"},
    )


@router.get("/download-upload/{filename}")
async def download_uploaded_file(
    filename: str,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
):
    """下载上传目录中的文件（只能下载自己上传的）"""
    # 检查权限（只能下载自己上传的，文件名前缀是 {user_id}_）
    if not filename.startswith(f"{current_user.id}_"):
        raise HTTPException(status_code=403, detail="无权访问该文件")

    rec = db.query(FileRecord).filter(
        FileRecord.user_id == current_user.id,
        FileRecord.kind == "upload",
        FileRecord.stored_name == filename,
    ).first()
    if rec:
        try:
            data = FileService(db).read_content(rec)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在")
        return Response(
            content=data,
            media_type=_guess_content_type(filename),
            headers=_disposition(filename),
        )
    # 遗留回退：本地 uploads 目录
    file_path = _safe_resolve(UPLOAD_DIR_STR, filename)
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=_guess_content_type(filename),
    )
