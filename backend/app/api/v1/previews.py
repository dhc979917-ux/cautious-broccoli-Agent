"""服务预览接口：查询 / 刷新 / 停止某条会话里发布的沙箱服务预览。

路由前缀用 /previews 而不是 /preview，避免和已有的
GET /sessions/{id}/preview（会话只读预览，文件中心浮窗用）撞语义。

权限口径：只能操作自己的预览（service_previews.user_id == 当前用户）；
管理员跨用户查看会话走 sessions 那套权限，这里不放开，减少一条越权面。
总开关 PREVIEW_ENABLED 关闭时接口返回空列表/404，前端卡片自然不渲染。
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from app.core.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import preview_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/previews", tags=["服务预览"])


@router.get("/sessions/{session_id}")
async def list_session_previews(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """列出某会话的服务预览（含已失效的历史记录，前端据此渲染卡片状态）。"""
    if not preview_service.preview_enabled():
        return {"enabled": False, "items": []}
    items: List[dict] = preview_service.list_previews(db, session_id, current_user.id)
    return {"enabled": True, "items": items}


@router.post("/{preview_id}/refresh")
async def refresh_session_preview(
    preview_id: int,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """重新探测沙箱：服务还活着就续期，已经没了就置为失效并说明原因。"""
    if not preview_service.preview_enabled():
        raise HTTPException(status_code=404, detail="服务预览未启用")
    item = await preview_service.refresh_preview(db, preview_id, current_user.id)
    if item is None:
        raise HTTPException(status_code=404, detail="预览不存在")
    return item


@router.post("/{preview_id}/stop")
async def stop_session_preview(
    preview_id: int,
    current_user: User = Depends(get_current_user),
    db: DbSession = Depends(get_db),
) -> dict:
    """停止预览：立刻摘掉代理容器，释放宿主端口。"""
    if not preview_service.preview_enabled():
        raise HTTPException(status_code=404, detail="服务预览未启用")
    item = await preview_service.stop_preview(db, preview_id, current_user.id)
    if item is None:
        raise HTTPException(status_code=404, detail="预览不存在")
    logger.info(f"[preview] 用户 {current_user.id} 停止了预览 {preview_id}")
    return item
