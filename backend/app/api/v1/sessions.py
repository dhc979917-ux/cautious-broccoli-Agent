import asyncio
import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List
from app.database import get_db
from app.services.session_service import SessionService
from app.schemas.session import (
    CreateSessionRequest, SessionResponse, 
    MessageResponse, SendMessageRequest
)
from app.core.deps import get_current_user, has_perm
from app.models.user import User
from app.models.agent import Agent
from app.models.session import Session as SessionModel
from app.services.task_manager import SessionBusyError, task_manager
from app.agents import pending_input
from app.services.model_access import available_catalog, effective_model
from app.services.context_compaction import compact_session


'''会话的列表、创建、查消息历史、删除'''


router = APIRouter(prefix="/sessions", tags=["会话管理"])

# 跨用户预览会话所需的功能权限（管理员天然满足）：文件中心与调度任务两处入口共用
_PREVIEW_PERMS = ("file_all.view", "monitor_manage")


def _serialize_messages(messages) -> list:
    """ORM 消息 -> 前端渲染格式（解析 metadata_json，把 steps/file_outputs/
    token_usage/attachments 一并返回）。历史接口与预览接口共用，保证渲染一致。"""
    import json
    result = []
    for msg in messages:
        meta = {}
        if msg.metadata_json:
            try:
                meta = json.loads(msg.metadata_json)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        result.append({
            "id": msg.id,
            "session_id": msg.session_id,
            "role": msg.role,
            "content": msg.content,
            "agent_name": msg.agent_name,
            "created_at": msg.created_at,
            "steps": meta.get("steps", []),
            "file_outputs": meta.get("file_outputs", []),
            "token_usage": meta.get("token_usage"),
            "run_id": meta.get("run_id"),
            "tool_call_count": meta.get("tool_call_count"),
            "attachments": meta.get("attachments", []),
            "elapsed_seconds": meta.get("elapsed_seconds"),
            # 服务预览卡片：刷新页面后按落库的 metadata 还原（状态可能是已过期的历史记录）
            "service_previews": meta.get("service_previews", []),
        })
    return result


def _can_preview(current_user: User, session) -> bool:
    """会话预览权限：属主放行；他人会话需具备任一跨用户查看权限"""
    if session.user_id == current_user.id:
        return True
    return any(has_perm(current_user, p) for p in _PREVIEW_PERMS)


@router.get("/", response_model=List[SessionResponse])
async def get_sessions(
    agent_id: int = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取当前用户的所有会话"""
    session_service = SessionService(db)
    # agent_id 可选：传入时只返回该 Agent 的会话（历史记录按 Agent 隔离）
    sessions = session_service.get_user_sessions(current_user.id, agent_id=agent_id)
    return sessions

@router.post("/", response_model=SessionResponse)
async def create_session(
    data: CreateSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """创建新会话"""
    # 检查Agent权限
    from app.services.agent_access import can_execute_agent
    if not can_execute_agent(db, current_user.id, data.agent_id, current_user.role):
        raise HTTPException(status_code=403, detail="无权访问该Agent")
    
    session_service = SessionService(db)
    session = session_service.create_session(
        user_id=current_user.id,
        agent_id=data.agent_id,
        title=data.title
    )
    return session

@router.get("/by-user/{user_id}", response_model=List[SessionResponse])
async def get_sessions_by_user(
    user_id: int,
    agent_id: int = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """管理员查看指定用户的会话列表（需 monitor_manage 权限）。

    与 GET /sessions/ 不同：这里按 user_id 查询他人的会话，仅限有监控权限的管理员。
    返回格式与 /sessions/ 一致，额外填充 agent_name 以便前端直接展示。
    """
    if not has_perm(current_user, "monitor_manage"):
        raise HTTPException(status_code=403, detail="无权查看其他用户的会话")
    session_service = SessionService(db)
    sessions = session_service.get_user_sessions(user_id, agent_id=agent_id)
    # 批量填充 agent_name（避免 N+1 查询）
    agent_ids = {s.agent_id for s in sessions}
    agent_map = {}
    if agent_ids:
        agents = db.query(Agent).filter(Agent.id.in_(agent_ids)).all()
        agent_map = {a.id: a.name for a in agents}
    result = [
        SessionResponse(
            id=s.id,
            agent_id=s.agent_id,
            agent_name=agent_map.get(s.agent_id),
            title=s.title,
            status=s.status,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]
    logger.info(f"[AUDIT] {current_user.username} 查看用户 {user_id} 的会话列表（{len(result)} 条）")
    return result

@router.get("/{session_id}/messages")
async def get_session_messages(
    session_id: int,
    limit: int = 50,
    before_id: int = None,
    include_usage: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取会话的消息历史（支持分页）
    
    - limit: 每次加载多少条（默认50）
    - before_id: 加载这个ID之前的消息（不传=加载最新的）
    - 返回: {messages: [...], has_more: bool}
    """
    session_service = SessionService(db)

    # 验证会话所有权（走 service 层，保持分层一致）
    try:
        session = session_service.get_session_by_id(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    messages, has_more = session_service.get_session_messages(session_id, limit=limit, before_id=before_id)
    
    # 转换为响应格式，让前端刷新后能用和刚发完消息时一样的格式渲染
    response = {
        "messages": _serialize_messages(messages),
        "has_more": has_more,
    }
    if include_usage:
        response["usage_totals"] = session_service.get_session_usage(session_id)
    return response


@router.get("/{session_id}/preview")
async def preview_session(
    session_id: int,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """会话只读预览：文件中心"来源"浮窗与调度任务执行历史共用。

    - 属主可预览自己的会话；他人会话需 file_all.view 或 monitor_manage（管理员天然满足）
    - 返回与 /messages 相同的富格式消息，前端可直接复用聊天气泡组件渲染
    - 会话已删除（物理删除）统一 404，前端据此显示空态
    """
    session_service = SessionService(db)
    try:
        session = session_service.get_session_by_id(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")
    if not _can_preview(current_user, session):
        raise HTTPException(status_code=403, detail="无权查看该会话")

    owner = db.query(User).filter(User.id == session.user_id).first()
    agent = db.query(Agent).filter(Agent.id == session.agent_id).first()
    messages, has_more = session_service.get_session_messages(
        session_id, limit=max(1, min(int(limit or 200), 500)))

    if session.user_id != current_user.id:
        logger.info(f"[AUDIT] {current_user.username} 预览用户 {session.user_id} 的会话 {session_id}")

    return {
        "session": {
            "id": session.id,
            "title": session.title,
            "agent_id": session.agent_id,
            "agent_name": agent.name if agent else None,
            "user_id": session.user_id,
            "username": owner.username if owner else None,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "is_own": session.user_id == current_user.id,
        },
        "messages": _serialize_messages(messages),
        "has_more": has_more,
    }

@router.delete("/{session_id}")
async def delete_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """删除会话"""
    session_service = SessionService(db)
    try:
        session = session_service.get_session_by_id(session_id)
        if session.user_id != current_user.id:
            raise ValueError("会话不存在或无权删除")
        if task_manager.is_running(session_id):
            from app.services.task_control import stop_session_task
            await stop_session_task(session_id)
            if task_manager.is_running(session_id):
                raise HTTPException(status_code=409, detail="会话任务仍在结束中，请稍后重试删除")
        session_service.delete_session(session_id, current_user.id)
        # 会话删除即清理其常驻 Agent 实例（记忆/沙箱/MCP 连接），不等到每日清理
        from app.agents.manager import agent_manager
        try:
            await agent_manager.evict_session(session_id)
        except Exception as e:
            logger.warning(f"[WARN] 会话 {session_id} Agent 实例清理失败（不影响删除）: {e}")
        from app.agents.offload_lifecycle import remove_session_offloads
        remove_session_offloads(current_user.id, session_id)
        return {"message": "会话已删除"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IntegrityError as e:
        db.rollback()
        logger.error(f"[ERROR] 会话 {session_id} 删除失败（数据完整性约束）: {e}")
        raise HTTPException(status_code=500, detail="删除失败：存在关联数据引用，请稍后重试")


@router.patch("/{session_id}/title")
async def update_session_title(
    session_id: int,
    title: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """更新会话标题"""
    session_service = SessionService(db)
    try:
        session = session_service.update_session_title(session_id, current_user.id, title)
        return {"message": "标题已更新", "title": session.title}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{session_id}/compress")
async def compress_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = db.query(SessionModel).filter(
        SessionModel.id == session_id,
        SessionModel.user_id == current_user.id,
    ).first()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    agent = db.get(Agent, session.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    from app.config import settings
    if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        from app.services.agent_versions import for_session
        agent = for_session(db, agent, session_id)
    model = effective_model(db, current_user, agent, catalog=await available_catalog())
    try:
        reservation = task_manager.reserve(session_id)
    except SessionBusyError as error:
        raise HTTPException(status_code=409, detail="会话有任务运行中，暂不能压缩") from error
    try:
        status = "done"
        result = await compact_session(db, session, model)
        return result
    except asyncio.CancelledError:
        status = "stopped"
        db.rollback()
        raise
    except Exception as error:
        status = "error"
        db.rollback()
        logger.warning("会话 %s 压缩失败: %s", session_id, error)
        raise HTTPException(status_code=502, detail="压缩失败，原上下文保持不变，请稍后重试") from error
    finally:
        await reservation.finish(status)


@router.get("/{session_id}/task-status")
async def get_task_status(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """查询会话是否有后台任务在运行（前端进入会话时据此决定是否续播流式输出）"""
    session_service = SessionService(db)
    try:
        session = session_service.get_session_by_id(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    info = task_manager.get(session_id)
    # 排队中的延时发送内容（任务运行中用户点发送挂起的内容），
    # 前端刷新/重进会话时据此恢复待发送区
    queued_inputs = [
        {
            "content": it.get("content", ""),
            "files": [
                {"path": f["path"], "name": f["name"], "stored_name": f["stored_name"]}
                for f in it.get("files") or []
            ],
        }
        for it in pending_input.peek(session_id)
    ]
    if not info:
        return {"running": False, "status": None, "queued_inputs": queued_inputs, "replay_from": 0}
    # replay_from：最后一次排队投递之后的事件下标，前端 resume 从这里回放，
    # 已分段落库的内容不重复渲染
    return {
        "running": info.status == "running",
        "status": info.status,
        "queued_inputs": queued_inputs,
        "replay_from": info.replay_from,
        "run_id": info.run_id,
        "token_usage": info.last_token_usage,
    }


@router.post("/{session_id}/branch", response_model=SessionResponse)
async def branch_session(
    session_id: int,
    from_message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """从此消息创建新会话：复制到该消息为止的全部消息，标题 = (分支)+原标题"""
    session_service = SessionService(db)
    try:
        new_session = session_service.branch_session(session_id, current_user.id, from_message_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return new_session


@router.delete("/{session_id}/messages/{message_id}")
async def undo_message(
    session_id: int,
    message_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """撤销用户消息：删除该消息及随后的 agent 回复，返回内容供前端回填输入框"""
    session_service = SessionService(db)
    try:
        result = session_service.delete_message_pair(session_id, message_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result
