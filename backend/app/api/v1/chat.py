import logging

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
import asyncio
import json
import os
import time

from app.database import get_db, SessionLocal
from app.config import settings
from app.agents import interaction
from app.agents.interaction import InteractionChannel, current_interaction
from app.agents.progress import current_progress
from app.agents import pending_input
from app.services.session_service import SessionService
from app.services.agent_service import AgentService
from app.services.skill_service import SkillService
from app.services.output_service import (
    get_session_output_dir,
    snapshot_dir,
    find_new_files,
    drop_redundant_archives,
    build_output_zip,
)
from app.services.file_service import FileService
from app.models.file import FileRecord
from app.models.message import Message
from app.schemas.session import SendMessageRequest, MessageResponse
from app.core.deps import get_current_user
from app.core.security import decode_token
from app.models.user import User
from app.services.agent_access import can_execute_agent
from app.agents.manager import agent_manager
from app.services.task_manager import SessionBusyError, task_manager
from app.agents.workspace import agent_visible_path
from app.core import presence
from app.services.task_control import stop_reason_of, stop_session_task
from app.services.model_access import available_catalog, effective_model


'''发消息给 Agent（REST 和 WebSocket 两种方式）'''

router = APIRouter(tags=["聊天"])


def _check_agent_permission(db: Session, user: User, agent_id: int):
    """校验当前账号是否可执行指定 Agent，无权抛 403"""
    if not can_execute_agent(db, user.id, agent_id, user.role):
        raise HTTPException(status_code=403, detail="无权访问该Agent")


def _materialize_attachment(db: Session, user_id: int, output_dir: str, stored_name: str):
    """把附件从文件中心（本地磁盘）复制到会话目录 _attachments/，返回本地路径供技能读取。
    失败返回 None（调用方回退旧路径，兼容遗留本地文件）。
    物化发生在输出目录快照之前，附件不会被误判为本轮产出。"""
    rec = db.query(FileRecord).filter(
        FileRecord.user_id == user_id,
        FileRecord.kind == "upload",
        FileRecord.stored_name == stored_name,
    ).first()
    if not rec:
        return None
    try:
        data = FileService(db).read_content(rec)
    except Exception as e:
        logger.warning(f"[WARN] 附件读取失败 {stored_name}: {e}")
        return None
    att_dir = os.path.join(output_dir, "_attachments")
    os.makedirs(att_dir, exist_ok=True)
    local_path = os.path.join(att_dir, stored_name)
    try:
        with open(local_path, "wb") as f:
            f.write(data)
    except OSError as e:
        logger.warning(f"[WARN] 附件落盘失败 {local_path}: {e}")
        return None
    return local_path


def _materialize_item_files(db: Session, user_id: int, output_dir: str, items: list):
    """把排队项里的附件从文件中心逐个物化到会话目录，
    并就地改写每个文件的 path 为本地路径（投递时 files_block 注入该路径）。
    物化失败的附件保留原 path（兼容遗留本地文件）。"""
    for it in items:
        for f in it.get("files") or []:
            stored = f.get("stored_name") or ""
            if not stored:
                continue
            local = _materialize_attachment(db, user_id, output_dir, stored)
            if local:
                # 注入给模型的必须是 Agent 实际能访问的路径：docker 沙箱下
                # 后端侧路径（/app/outputs/...）在沙箱里不存在，需翻译成 /workspace/...
                f["path"] = agent_visible_path(local, output_dir)


def _hydrate_session_files(user_id: int, session_id: int, output_dir: str):
    """任务启动前，把会话历史产出与历史附件回补到本地会话目录。
    本地已存在且大小一致的跳过；失败只 WARN 不阻断。
    必须在输出目录快照之前调用，回补的文件不会被误判为本轮产出。"""
    db = SessionLocal()
    try:
        fs = FileService(db)
        # 1) 历史产出：sessions/{sid}/ 前缀的 output 记录
        prefix = f"sessions/{session_id}/"
        recs = db.query(FileRecord).filter(
            FileRecord.user_id == user_id,
            FileRecord.kind == "output",
            FileRecord.stored_name.like(prefix + "%"),
            FileRecord.deleted_at.is_(None),
        ).all()
        for rec in recs:
            rel = rec.stored_name[len(prefix):]
            local_path = os.path.join(output_dir, *rel.split("/"))
            if os.path.exists(local_path) and os.path.getsize(local_path) == (rec.size or 0):
                continue  # 本地缓存命中
            try:
                data = fs.read_content(rec)
            except Exception as e:
                logger.warning(f"[WARN] 历史产出水合失败 {rec.stored_name}: {e}")
                continue
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            try:
                with open(local_path, "wb") as f:
                    f.write(data)
            except OSError as e:
                logger.warning(f"[WARN] 历史产出落盘失败 {local_path}: {e}")
        # 2) 历史附件：用户消息 metadata 里登记过的上传文件
        att_dir = os.path.join(output_dir, "_attachments")
        msgs = db.query(Message).filter(
            Message.session_id == session_id, Message.role == "user"
        ).all()
        seen = set()
        for m in msgs:
            if not m.metadata_json:
                continue
            try:
                meta = json.loads(m.metadata_json)
            except ValueError:
                continue
            for att in meta.get("attachments") or []:
                stored = att.get("stored_name")
                if not stored or stored in seen:
                    continue
                seen.add(stored)
                local_path = os.path.join(att_dir, stored)
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    continue
                _materialize_attachment(db, user_id, output_dir, stored)
    finally:
        db.close()


def _final_session_cleanup(session_id: int):
    """任务收尾：Skill 单次访问注册表清理（防进程内无限增长）"""
    try:
        from app.agents.middlewares.skill_once import clear_session
        clear_session(str(session_id))
    except Exception:
        pass


def _detect_and_register_outputs(
    db,
    user_id: int,
    session_id: int,
    agent_id: int,
    output_dir: str,
    before_snapshot: dict,
) -> list:
    """对比快照找出本次运行新生成/变化的文件，登记文件中心，返回 file_outputs 列表。

    后台任务三个出口（正常结束 / 用户停止 / 异常失败）共用：纯同步实现，
    取消/异常分支里调用也安全；任一步失败只返回已检测到的部分，不阻断主流程。
    """
    try:
        new_files = find_new_files(output_dir, before_snapshot)
    except Exception as e:
        logger.warning(f"[WARN] 产出文件检测失败: {e}")
        return []
    if not new_files:
        return []
    # 模型自打的压缩包若只是把本轮原文件重复包一遍，剔掉它（交付口径见
    # output_service.drop_redundant_archives）。放在阈值打包之前，计数才准。
    try:
        new_files = drop_redundant_archives(output_dir, new_files)
    except Exception as e:
        logger.warning(f"[WARN] 自打包去重失败（按原样交付）: {e}")
    if not new_files:
        return []
    # 产出文件打包：超过阈值时打包为单个 zip，避免文件中心被大量文件淹没。
    # 阈值通过 settings.OUTPUT_ZIP_THRESHOLD 配置（默认 3），打包失败则回退为单文件模式。
    _zip_threshold = getattr(settings, 'OUTPUT_ZIP_THRESHOLD', 3)
    if len(new_files) > _zip_threshold:
        zip_name = f"outputs_{int(time.time())}.zip"
        # 打包实现见 output_service.build_output_zip：时间戳夹到 1980 之后、
        # 单文件容错、原子落盘。整包失败才回退单文件模式（此前一个 mtime=0 的
        # 文件就会让整包炸掉，几十上百个碎文件淹掉文件中心）。
        zip_item, pack_errors = build_output_zip(output_dir, new_files, zip_name)
        if zip_item is not None:
            logger.info(
                f"[产出打包] 会话 {session_id}: {len(new_files)} 个文件 -> {zip_name} "
                f"({zip_item['size']} bytes)"
            )
            new_files = [zip_item]
        else:
            logger.warning(
                f"[WARN] 产出文件打包失败，回退为单文件模式: {'; '.join(pack_errors[:3])}"
            )
    # 文件位于会话子目录内；登记/下载统一用相对用户目录的路径（带 sessions/{id}/ 前缀）
    rel_prefix = f"sessions/{session_id}/"
    file_outputs = [
        {"filename": rel_prefix + f["filename"], "download_url": f"/api/v1/upload/download/{rel_prefix}{f['filename']}", "size": f["size"]}
        for f in new_files
    ]
    # 文件中心：产出文件登记元数据（带会话/Agent 溯源；失败不影响聊天主流程）
    try:
        fs = FileService(db)
        for f in new_files:
            fs.register_output(
                user_id=user_id,
                filename=rel_prefix + f["filename"],
                size=f["size"],
                session_id=session_id,
                agent_id=agent_id,
            )
    except Exception as e:
        logger.warning(f"[WARN] 产出文件登记失败（不影响聊天）: {e}")
    return file_outputs


async def _publish_service_previews(
    db,
    user_id: int,
    session_id: int,
    agent_id: int,
    output_dir: str,
    emit,
) -> list:
    """收尾阶段：探测沙箱里 Agent 起的服务，发布成用户可直接点开的临时地址。

    返回要落进消息 metadata 的预览列表（可能为空）。整段属于增强项：
    总开关关闭 / 非 docker 沙箱 / Agent 已淘汰 / 没探测到监听端口时立即返回，
    单条发布失败只跳过那一条，绝不影响本轮回复的落库与投递。
    """
    from app.services import preview_service
    if not preview_service.preview_enabled():
        return []
    container_name = agent_manager.get_sandbox_container_name(session_id)
    if not container_name:
        return []
    from app.agents.service_probe import detect_services
    from app.agents.workspace import SANDBOX_CONTAINER_PREFIX
    services = await detect_services(container_name)
    if not services:
        return []
    limit = max(1, int(getattr(settings, "PREVIEW_MAX_PER_TURN", 3) or 1))
    workspace_id = container_name[len(SANDBOX_CONTAINER_PREFIX):]
    results = []
    for svc in services[:limit]:
        try:
            item = await preview_service.publish_preview(
                db,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                container_name=container_name,
                workspace_id=workspace_id,
                service=svc,
                output_dir=output_dir,
            )
        except Exception as e:
            logger.warning(f"[WARN] 会话 {session_id} 预览发布失败（端口 {svc.port}）: {e}")
            continue
        if not item:
            continue
        results.append(item)
        try:
            await emit({"type": "service_preview", "preview": item})
        except Exception:
            pass
    return results


def _sanitize_reply(full_reply: str, previews: list) -> str:
    """把回复里的沙箱内部地址换成平台发布的真实预览地址。

    模型看不到宿主网络，起完服务习惯性地把 http://localhost:8123 交给用户，
    用户点开必然失败（会话 398 的原始故障）。提示词已禁止但遵从性不稳定，
    落库前再做一次确定性兜底；净化本身失败就按原文落库，不影响回复。
    """
    if not full_reply:
        return full_reply
    try:
        from app.agents.middlewares.reply_sanitizer import sanitize_reply
        from app.services.preview_service import public_base_url
        return sanitize_reply(full_reply, previews or [], base_url=public_base_url())
    except Exception as e:
        logger.warning(f"[WARN] 回复净化失败（按原文落库）: {e}")
        return full_reply


@router.post("/chat", response_model=MessageResponse)
async def send_message(
    data: SendMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    session_service = SessionService(db)
    agent_service = AgentService(db)

    # 第一步：解析会话（传了 session_id 就校验归属；没传就用 agent_id 创建新会话）
    if data.session_id:
        try:
            session = session_service.get_session_by_id(data.session_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != current_user.id:
            # 不能往别人的会话里写消息
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        if data.agent_id is None:
            raise HTTPException(status_code=400, detail="创建新会话时 agent_id 必填")
        _check_agent_permission(db, current_user, data.agent_id)
        session = session_service.create_session(
            user_id=current_user.id,
            agent_id=data.agent_id
        )

    # 第二步：获取 Agent 配置 + 权限检查（先做鉴权，再落库任何数据）
    try:
        agent_model = agent_service.get_agent_by_id(session.agent_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Agent不存在")
    _check_agent_permission(db, current_user, agent_model.id)
    if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        from app.services.agent_versions import for_session
        agent_model = for_session(db, agent_model, session.id)
    catalog = await available_catalog()
    data.model = effective_model(db, current_user, agent_model, data.model, catalog)

    try:
        reservation = task_manager.reserve(session.id)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="该会话有任务正在运行") from None
    reservation.user_id = current_user.id
    try:
        result = await _send_rest_message(data, current_user, db, session, agent_model)
    except asyncio.CancelledError:
        await reservation.finish("stopped")
        raise
    except Exception:
        await reservation.finish("error")
        raise
    await reservation.finish("done")
    return result


async def _send_rest_message(data, current_user, db, session, agent_model):
    session_service = SessionService(db)
    skill_service = SkillService(db)

    # 第三步：保存用户消息
    session_service.add_message(
        session_id=session.id,
        role="user",
        content=data.content
    )

    # 第四步：调 AI 引擎
    # agent_manager.chat 是异步方法（内部消费 chat_stream），直接 await
    # 技能白名单以数据库为准（is_active + 角色 + 用户授权 + 隐藏规则），管理端改动即时生效
    from app.services.skill_runtime import validate_selection, prepare_agent
    from app.services.skill_evolution.runtime import selection as evolution_selection
    selected_skills = evolution_selection(db, current_user, session.id, data.selected_skills)
    if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        from app.services.agent_versions import for_session
        agent_model = prepare_agent(db, for_session(db, agent_model, session.id), selected_skills, current_user.role, current_user.id)
    try:
        reply_text = await agent_manager.chat(
            agent_model,
            data.content,
            current_user.role,
            selected_skills=selected_skills,
            user_id=current_user.id,
            model=data.model,
            session_id=session.id,
            selected_mcps=data.selected_mcps,
            structured_schema=data.structured_schema,
            db=db,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent处理失败: {str(e)}")

    # Post-process: append citation mapping from SearchKnowledge tool
    from app.agents.tools.search_knowledge import pop_citation_map
    _cite_map = pop_citation_map(session.id)
    if _cite_map:
        mapping = ";".join(f"{k}={v}" for k, v in sorted(_cite_map.items()))
        reply_text += f"\n\n引用映射:{mapping}"

    # 第五步：保存 AI 回复并返回
    agent_msg = session_service.add_message(
        session_id=session.id,
        role="agent",
        content=reply_text,
        agent_name=agent_model.name,
        metadata={"model": data.model,
                  "agent_version_id": (agent_model.config or {}).get("_agent_version_id"),
                  "skill_versions": (agent_model.config or {}).get("_runtime_skill_versions", {})}
    )

    from app.services.skill_evolution.worker import safe_enqueue
    safe_enqueue(db, user_id=current_user.id, session_id=session.id,
        message_id=agent_msg.id, model=data.model or agent_model.model_config_name or settings.MODEL_NAME,
        selected_skills=selected_skills, versions=(agent_model.config or {}).get('_runtime_skill_versions', {}))
    return agent_msg

# 浏览器 WebSocket 使用子协议传递 JWT；旧客户端的查询参数保留向后兼容。
@router.websocket("/ws/chat/{session_id}")
async def websocket_chat(
    websocket: WebSocket,
    session_id: int,
    token: str = "",
):
    # 第一步：验证令牌（WebSocket 不走 FastAPI 的依赖注入体系，直接调 decode_token）
    protocols = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    protocol_token = next((item[4:] for item in protocols if item.startswith("jwt.")), "")
    token = protocol_token or token
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001, reason="无效的认证令牌")
        return

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        await websocket.close(code=4001, reason="无效的认证令牌")
        return

    # 第二步：接受连接 + 准备 Service
    await websocket.accept(subprotocol="taskpilot-chat" if "taskpilot-chat" in protocols else None)
    logger.info(f"[WebSocket] 连接成功, session_id={session_id}")


    # 事件转发协程（与接收循环并行运行，详见循环处说明）
    fwd_task = None
    try:
        # 第三步：连接期校验用短会话（校验完即归还，长连接生命周期不再持有 db）：
        # 以数据库为准加载用户/会话/Agent（角色/权限/状态），不用 JWT 里的旧快照
        with SessionLocal() as db:
            session_service = SessionService(db)
            agent_service = AgentService(db)
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not user.is_active:
                await websocket.close(code=4001, reason="用户不存在或已被禁用")
                return
            # 令牌吊销校验：管理员改过该用户的角色/权限后旧 token 立即失效（口径同 core/deps.py）
            if int(payload.get("tv") or 0) != int(user.token_version or 0):
                await websocket.close(code=4001, reason="登录状态已失效，请重新登录")
                return
            if bool(user.must_change_password):
                await websocket.close(code=4003, reason="首次登录必须先修改密码")
                return
            user_role = user.role

            # 第四步：验证会话所有权 + Agent 权限
            session = session_service.get_session_by_id(session_id)
            if not session or session.user_id != user_id:
                await websocket.close(code=4004, reason="会话不存在")
                return

            agent_model = agent_service.get_agent_by_id(session.agent_id)
            if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
                from app.services.agent_versions import for_session
                agent_model = for_session(db, agent_model, session.id)
            if not can_execute_agent(db, user.id, agent_model.id, user.role):
                await websocket.close(code=4003, reason="无权访问该Agent")
                return

            logger.info(f"[WebSocket] Agent: {agent_model.name}")

        # 该会话的专属输出子目录（会话级隔离：多会话并行时文件产出互不串归属）
        output_dir = get_session_output_dir(user_id, session_id)

        # 登记在线连接：管理端吊销会话时按用户推 force_logout 并关闭连接（core/presence.py）
        presence.register(user_id, websocket)

        # 第五步：循环监听消息
        while True:
            # 收割已完成的转发任务（客户端断开等异常静默处理，外层 receive 会抛错退出）
            if fwd_task is not None and fwd_task.done():
                try:
                    fwd_task.result()
                except (WebSocketDisconnect, RuntimeError):
                    pass
                except Exception as e:
                    logger.warning(f"[WebSocket] 事件转发异常: {e}")
                fwd_task = None

            data = await websocket.receive_text()

            # 单条消息解析失败不应杀死整个连接
            try:
                message_data = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "消息格式错误（不是合法JSON）"})
                continue

            mtype = message_data.get("type")

            if mtype == "external_reply":
                channel = interaction.get_channel(session_id)
                rid = str(message_data.get("tool_call_id") or message_data.get("request_id") or "")
                ok = False
                if channel is not None and rid:
                    if message_data.get("skipped"):
                        ok = await channel.cancel(rid)
                    else:
                        answer = message_data.get("answer_text") or message_data.get("answer") or ""
                        ok = await channel.resolve(rid, str(answer))
                if not ok:
                    # 业务性错误（决策卡过期/任务已结束）：带 kind 标记，前端只提示不断连
                    await websocket.send_json({
                        "type": "error",
                        "kind": "external_reply_miss",
                        "content": "未找到对应的外部执行请求（可能已超时或已处理）",
                    })
                continue

            # 续播回放：前端进入会话时若检测到后台任务（运行中/刚结束），
            # 发送 type=resume 重放事件流，恢复流式显示
            if mtype == "resume":
                if fwd_task is not None and not fwd_task.done():
                    continue  # 已在转发中，忽略重复 resume
                info = task_manager.get(session_id)
                if not info:
                    await websocket.send_json({"type": "error", "content": "该会话当前没有后台任务"})
                    continue
                from_index = int(message_data.get("from_index") or 0)
                fwd_task = asyncio.create_task(_forward_task_events(websocket, info, from_index))
                continue

            # 排队（延时发送）：任务运行中前端把完整排队快照整体替换过来，
            # 由后台任务在下一轮模型调用时注入给模型。无运行中任务时拒绝并
            # 回传 items，前端改走普通发送
            if mtype == "queue_input":
                items = pending_input.normalize_items(message_data.get("items") or [])
                with SessionLocal() as db:
                    _materialize_item_files(db, user_id, output_dir, items)
                if task_manager.is_running(session_id):
                    pending_input.set_queue(session_id, items)
                    await websocket.send_json({"type": "queued", "queue_size": len(items)})
                else:
                    await websocket.send_json({
                        "type": "queued_rejected",
                        "reason": "no_running_task",
                        "items": items,
                    })
                continue

            # 停止当前任务：Esc 触发。纯停止语义——停任务、丢弃排队内容，
            # 不再把队列/草稿自动续发为新任务；前端收到 stop_ack 后清掉本轮全部状态。
            # 无运行中任务也正常回执（前端用 Esc 清理残留决策卡等本地状态）。
            if mtype == "stop":
                # 停止语义只有一份实现（services/task_control.py）：管理端强制下线也走它，
                # 避免两处各写一套后彼此漂移
                await stop_session_task(session_id)
                # 等旧任务的事件转发结束，避免新旧事件在连接上交错；
                # 限时等待：转发卡住时放弃等待，不能把接收循环挂死（Esc 卡死的根源之一）
                if fwd_task is not None and not fwd_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(fwd_task), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"[WebSocket] 会话 {session_id} 停止：事件转发 5s 未结束，放弃等待")
                    except (WebSocketDisconnect, RuntimeError):
                        raise WebSocketDisconnect()
                    except Exception as e:
                        logger.warning(f"[WebSocket] 事件转发异常: {e}")
                fwd_task = None
                # 排队内容随停止丢弃（纯停止：用户明确要求停下并清状态）
                pending_input.drain(session_id)
                await websocket.send_json({"type": "stop_ack", "items": []})
                continue

            # 每会话同一时间只允许一个任务在跑：运行中拒绝新提交
            if task_manager.is_running(session_id):
                _cur_info = task_manager.get(session_id)
                if _cur_info is not None and _cur_info.cancel_requested:
                    # Esc 停止已请求但旧任务尚未到达终态：短暂等待收尾完成再接收新提交，
                    # 避免"停止后立即再发送"撞上取消窗口被误拒
                    await task_manager.wait_finished(session_id, timeout=10.0)
                if task_manager.is_running(session_id):
                    await websocket.send_json({"type": "error", "content": "该会话有任务正在运行，请等待完成后再发送"})
                    continue

            user_content = message_data.get("content", "")
            # 不将 [] 转换为 None：空列表 = 不加载任何可选技能（内置技能不受影响）
            # None 仅在旧客户端省略字段时出现，下方逻辑回退为全部可选技能（向后兼容）
            selected_skills = message_data.get("selected_skills")
            # Per-message MCP subset (None = all MCPs bound to the Agent)
            selected_mcps = _normalize_mcp_ids(message_data.get("selected_mcps"))
            # 用户自选模型（可选）：不传则用 Agent 的 DB 配置模型
            model = message_data.get("model")
            # 结构化输出要求（可选）：JSON Schema dict，透传 AgentScope reply_stream
            structured_schema = message_data.get("structured_schema")
            if not isinstance(structured_schema, dict):
                structured_schema = None

            # 每消息短会话：技能白名单计算 + 附件物化 + 用户消息落库（处理完即归还连接）
            with SessionLocal() as db:
                session_service = SessionService(db)
                # 技能白名单以数据库为准（is_active + 角色 + 用户授权 + 隐藏规则），
                # 再与前端勾选取交集，使管理端的权限/授权/隐藏修改即时生效
                ws_user = db.query(User).filter(User.id == user_id).first()
                if ws_user is None or not ws_user.is_active:
                    await websocket.send_json({"type": "error", "content": "账号不可用"})
                    continue
                try:
                    catalog = await available_catalog()
                    model = effective_model(db, ws_user, agent_model, model, catalog)
                except (HTTPException, RuntimeError) as error:
                    await websocket.send_json({"type": "error", "content": str(getattr(error, "detail", error))})
                    continue
                try:
                    from app.services.skill_runtime import validate_selection
                    from app.services.skill_evolution.runtime import selection as evolution_selection
                    selected_skills = evolution_selection(db, ws_user, session_id, selected_skills)
                except HTTPException as error:
                    await websocket.send_json({'type': 'error', 'content': str(error.detail)})
                    continue

                # 提取上传的文件（兼容 {path,name,stored_name} 对象和旧的纯路径字符串）
                attached_files = message_data.get("files", [])
                agent_content = user_content        # 发给 Agent 的内容（含文件路径注入）
                attachments_meta = []                # 存数据库/前端展示用的附件信息

                # 如果有附件，把文件路径注入"发给 Agent 的内容"，但不污染保存给用户的原文
                if attached_files:
                    file_info = "\n\n[用户附带了以下文件]\n"
                    for f in attached_files:
                        if isinstance(f, dict):
                            # 兼容两种键名：path/file_path, name/original_name
                            path = str(f.get("path") or f.get("file_path") or "")
                            name = str(f.get("name") or f.get("original_name") or os.path.basename(path))
                            stored = str(f.get("stored_name") or os.path.basename(path))
                        else:
                            path = str(f)
                            name = os.path.basename(path)
                            stored = name
                        # 从文件中心（本地磁盘）物化附件到会话目录再注入本地路径；
                        # 物化失败回退前端传来的旧路径（遗留本地文件仍可读取）
                        local_path = _materialize_attachment(db, user_id, output_dir, stored) or path
                        # 注入 Agent 实际能访问的路径（docker 沙箱下为 /workspace/...）
                        file_info += f"- {name} (完整路径: {agent_visible_path(local_path, output_dir)})\n"
                        attachments_meta.append({"name": name, "stored_name": stored})
                    file_info += (
                        "\n请使用对应的技能工具处理这些文件，输出结果请保存到 outputs 目录。\n"
                        "路径请直接复制使用，不要凭记忆改写（文件名含长哈希，抄错会找不到文件）；"
                        "若提示文件不存在，先 ls 该目录核对实际文件名，不要直接回复用户“没有附件”。\n"
                    )
                    agent_content = user_content + file_info

                # 第六步：保存用户消息
                # content 只存干净的用户原文，附件信息放 metadata_json（刷新后用来渲染附件卡片）
                session_service.add_message(
                    session_id=session_id,
                    role="user",
                    content=user_content,
                    metadata={"attachments": attachments_meta} if attachments_meta else None
                )

                # 自动标题：如果是第一条消息（消息总数为 1），截取前 20 个字符作为会话标题
                # 注意 get_session_messages 返回 (messages, has_more) 元组，需要解包
                messages, _ = session_service.get_session_messages(session_id, limit=10)
                if len(messages) == 1:
                    title_source = user_content.strip() or (attachments_meta[0]["name"] if attachments_meta else "新对话")
                    title = title_source[:20] + ("..." if len(title_source) > 20 else "")
                    try:
                        session_service.update_session_title(session_id, user_id, title)
                    except Exception:
                        pass

            # 第七步：启动后台任务执行 Agent，并转发事件流
            # 任务在服务端独立运行（TaskManager 托管）：用户切走页面、断开 WebSocket
            # 都不会中断执行，跑完照常落库；重新进入会话前端用 type=resume 续播
            # 先对输出目录做快照，运行结束后对比找出本次真正新生成的文件
            info = _start_agent_task(
                session_id=session_id,
                agent_model=agent_model,
                agent_content=agent_content,
                user_role=user_role,
                selected_skills=selected_skills,
                selected_mcps=selected_mcps,
                user_id=user_id,
                model=model,
                output_dir=output_dir,
                structured_schema=structured_schema,
            )
            # 事件转发与接收循环并行：转发若占用循环，external_reply 等用户回应
            # 就无法送达，决策交互会死锁
            fwd_task = asyncio.create_task(_forward_task_events(websocket, info, 0))

    # 客户端主动断开（关闭页面、网络中断），这是正常的终止信号，无需额外处理
    except WebSocketDisconnect:
        logger.info(f"[WebSocket] 用户 {user_id} 断开连接")
    except Exception as e:
        logger.error(f"[WebSocket] 错误: {e}")
    finally:
        presence.unregister(user_id, websocket)
        if fwd_task is not None and not fwd_task.done():
            fwd_task.cancel()


def _normalize_mcp_ids(raw):
    """selected_mcps -> list[int] | None（None = 不限制，用 Agent 绑定的全部启用 MCP）"""
    if raw is None:
        return None
    try:
        return [int(i) for i in raw]
    except (TypeError, ValueError):
        return None


def _start_agent_task(
    session_id: int,
    agent_model,
    agent_content: str,
    user_role: str,
    selected_skills: list,
    selected_mcps: list,
    user_id: int,
    model,
    output_dir: str,
    structured_schema: dict = None,
):
    """启动后台 Agent 任务（普通发送 / stop 后立即续发 共用）。

    任务在服务端独立运行（TaskManager 托管）：用户切走页面、断开 WebSocket
    都不会中断执行，跑完照常落库；重新进入会话前端用 type=resume 续播。
    先对输出目录做快照，运行结束后对比找出本次真正新生成的文件。
    """
    # 清掉上一任务可能残留的排队项：排队内容随下一轮模型调用投递，
    # 前端对未投递项会在任务结束后重新发送，这里不清会导致新任务重复注入
    pending_input.clear(session_id)
    # 启动前把会话历史产出/附件回补到本地会话目录（快照之前，
    # 回补文件不会被误判为本轮产出）；失败不阻断任务，用本地现有文件继续
    try:
        _hydrate_session_files(user_id, session_id, output_dir)
    except Exception as e:
        logger.warning(f"[WARN] 会话文件回补失败（使用本地现有文件继续）: {e}")
    before_snapshot = snapshot_dir(output_dir)
    msg_start_ts = time.time()  # 本条消息开始时间，用于统计回复用时
    worker = _make_agent_worker(
        session_id=session_id,
        agent_model=agent_model,
        agent_content=agent_content,
        user_role=user_role,
        selected_skills=selected_skills,
        selected_mcps=selected_mcps,
        user_id=user_id,
        model=model,
        output_dir=output_dir,
        before_snapshot=before_snapshot,
        msg_start_ts=msg_start_ts,
        structured_schema=structured_schema,
    )
    info = task_manager.start(session_id, worker)
    # 记录本任务的模型/技能快照：stop 续发新任务时前端没传就沿用
    info.model = model
    info.selected_skills = selected_skills
    info.selected_mcps = selected_mcps
    info.structured_schema = structured_schema
    # 归属用户：管理端按用户批量停任务时用（task_manager.running_session_ids）
    info.user_id = user_id
    return info


def _make_agent_worker(
    session_id: int,
    agent_model,
    agent_content: str,
    user_role: str,
    selected_skills: list,
    selected_mcps: list,
    user_id: int,
    model,
    output_dir: str,
    before_snapshot: dict,
    msg_start_ts: float,
    structured_schema: dict = None,
):
    """构造后台任务协程（工厂）。emit 是 TaskManager 提供的事件写入回调。

    与 WebSocket 连接完全解耦：连接断开不影响执行，执行完成照常把回复落库。
    """

    async def worker(emit):
        nonlocal agent_model
        db = SessionLocal()  # 后台任务用独立数据库连接（发起方的连接可能已归还）
        session_service = SessionService(db)
        # ask_user 交互通道：工具侧经 contextvar 读取，WS 侧经 registry 按会话查找
        channel = InteractionChannel(session_id=session_id, emit=emit, user_id=user_id)
        interaction.register_channel(channel)
        cv_token = current_interaction.set(channel)
        # 构建阶段进度通道：Agent build（记忆库/MCP/沙箱）期间向前端推送
        # status 事件（转圈提示），避免长时间无事件让用户误以为卡死
        async def _progress_sink(text: str):
            await emit({"type": "status", "content": text})

        prog_token = current_progress.set(_progress_sink)
        # 实际使用的模型（落库到消息 metadata，供监控与运维按模型聚合）
        actual_model = model or agent_model.model_config_name or settings.MODEL_NAME
        version_trace = ({"agent_version_id": (agent_model.config or {}).get("_agent_version_id"),
                          "skill_versions": {}}
                         if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED else {})
        task_info = task_manager.get(session_id)
        run_id = task_info.run_id if task_info else None
        try:
            full_reply = ""
            collected_steps = []      # 收集思考/工具步骤，存入 metadata_json（刷新后还原格式）
            token_usage = None
            tool_call_count = 0
            tool_error_count = 0
            structured_output = None
            quality_metrics_snapshot = None   # 交付档位 + 采集/验证计数（监控聚合用）
            file_outputs = []         # 提前初始化：流式阶段之前若抛异常，except 分支也能安全引用
            service_previews = []     # 同上：服务预览在三个出口都要能安全引用
            # ask_user 记录去重：collected_steps 会因分段落库被重置，记录按 request_id 去重，
            # 保证每段落库时只追加尚未记录过的交互
            recorded_ask_ids = set()

            def _append_ask_records():
                # 官方外部执行记录：随分段同步进 steps（刷新后可还原决策卡/需求表单）
                for rec in channel.records.values():
                    if rec["request_id"] in recorded_ask_ids:
                        continue
                    recorded_ask_ids.add(rec["request_id"])
                    _step = {
                        "type": "external_ask",
                        "request_id": rec["request_id"],
                        "reply_id": rec.get("reply_id", ""),
                        "question": rec["question"],
                        "options": rec["options"],
                        "answer": rec["answer"],
                        "resolved_by": rec["resolved_by"],
                    }
                    # kind=intake（CollectRequirements）时带题目定义，前端渲染成表单
                    if rec.get("kind"):
                        _step["kind"] = rec["kind"]
                    if rec.get("questions"):
                        _step["questions"] = rec["questions"]
                    collected_steps.append(_step)

            try:
                if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
                    from app.services.agent_versions import for_session
                    from app.services.skill_runtime import prepare_agent
                    agent_model = prepare_agent(db, for_session(db, agent_model, session_id), selected_skills, user_role, user_id)
                    version_trace['skill_versions'] = agent_model.config['_runtime_skill_versions']
                # agent_manager.chat_stream 是异步生成器，每产生一个事件就 yield 一个字典
                async for event_data in agent_manager.chat_stream(
                    agent_model, agent_content,
                    user_role=user_role,
                    selected_skills=selected_skills,
                    selected_mcps=selected_mcps,
                    user_id=user_id,
                    model=model,
                    session_id=session_id,
                    structured_schema=structured_schema,
                    db=db,
                ):
                    event_type = event_data.get("event", "")

                    if event_type == "chunk":
                        # 文本chunk → 累加到完整回复 + 写入事件流
                        full_reply += event_data.get("content", "")
                        await emit({"type": "chunk", "content": event_data.get("content", "")})

                    elif event_type == "thinking":
                        # 连续的思考增量合并为同一段（修复一个 token 一行的问题）；
                        # 被工具调用打断后，下一段思考自然新开一条 step
                        thinking_delta = event_data.get("content", "")
                        if collected_steps and collected_steps[-1]["type"] == "thinking":
                            collected_steps[-1]["content"] += thinking_delta
                        else:
                            collected_steps.append({"type": "thinking", "content": thinking_delta})
                        await emit({"type": "thinking", "content": thinking_delta})

                    elif event_type == "tool_call":
                        _status = event_data.get("status", "")
                        if _status == "calling":
                            collected_steps.append({
                                "type": "tool_call",
                                "tool_call_id": event_data.get("tool_call_id", ""),
                                "name": event_data.get("name", ""),
                                "args": event_data.get("args", ""),
                                # 一句话描述（后端已把工具参数解析成人类可读，刷新后可还原）
                                "description": event_data.get("description", ""),
                                # 模型调用轮次：前端计划面板按轮次分组为"阶段 N"
                                "round": event_data.get("round"),
                                "status": "calling",
                            })
                        elif _status == "done":
                            for _s in reversed(collected_steps):
                                if _s["type"] == "tool_call" and _s["status"] == "calling" and _s.get("tool_call_id") == event_data.get("tool_call_id"):
                                    _s["status"] = "done"
                                    break
                        await emit({
                            "type": "tool_call",
                            "tool_call_id": event_data.get("tool_call_id", ""),
                            "name": event_data.get("name", ""),
                            "args": event_data.get("args", ""),
                            "description": event_data.get("description", ""),
                            "round": event_data.get("round"),
                            "status": _status,
                        })

                    elif event_type == "tool_result":
                        # 工具实际输出：挂到对应 tool_call step（刷新后可还原），同时转发前端展示
                        _n = event_data.get("name", "")
                        _tid = event_data.get("tool_call_id", "")
                        _out = event_data.get("output", "")
                        _state = event_data.get("state", "")
                        for _s in reversed(collected_steps):
                            if _s["type"] == "tool_call" and _s.get("tool_call_id") == _tid and _tid:
                                _s["output"] = _out
                                _s["status"] = "done"
                                if _state:
                                    _s["state"] = _state
                                break
                        await emit({"type": "tool_result", "tool_call_id": _tid, "name": _n, "output": _out, "state": _state})

                    elif event_type == "require_external_execution":
                        # 官方外部执行事件：前端直接按 tool_calls 渲染 AskUserCard；
                        # 决策记录由 _append_ask_records 以 external_ask step 落库
                        await emit({
                            "type": "require_external_execution",
                            "reply_id": event_data.get("reply_id", ""),
                            "tool_calls": event_data.get("tool_calls", []),
                            "timeout_seconds": event_data.get("timeout_seconds"),
                        })

                    elif event_type == "tasks":
                        # 官方 Task 工具快照：steps 中只保留最新一份（刷新后还原计划面板）
                        _tasks = event_data.get("tasks", [])
                        _found = False
                        for _s in collected_steps:
                            if _s.get("type") == "tasks":
                                _s["tasks"] = _tasks
                                _found = True
                                break
                        if not _found:
                            collected_steps.append({"type": "tasks", "tasks": _tasks})
                        await emit({"type": "tasks", "tasks": _tasks})

                    elif event_type == "usage":
                        token_usage = event_data.get("token_usage")
                        if task_info is not None:
                            task_info.last_token_usage = token_usage
                        await emit({
                            "type": "usage",
                            "run_id": run_id,
                            "model_call_rounds": event_data.get("model_call_rounds", 0),
                            "token_usage": token_usage,
                        })

                    elif event_type == "done":
                        token_usage = event_data.get("token_usage", {})
                        tool_call_count = event_data.get("tool_call_count", 0)
                        tool_error_count = event_data.get("tool_error_count", 0)
                        structured_output = event_data.get("structured_output")
                        quality_metrics_snapshot = event_data.get("quality")
                        await emit({
                            "type": "done",
                            "run_id": run_id,
                            "token_usage": token_usage,
                            "tool_call_count": tool_call_count,
                            "tool_error_count": tool_error_count,
                            "structured_output": structured_output,
                            "quality": quality_metrics_snapshot,
                        })

                    elif event_type == "queued_delivered":
                        # 排队输入已在下一轮模型调用前注入上下文。
                        # 分段落库：先把"到此为止"已产出的部分冻结为一条 agent 消息，
                        # 再把每条排队项各落一条用户消息（metadata.queued=True），
                        # 然后重置累积器，后续事件进入新的一段——与前端分段展示对应，
                        # 刷新后布局为：…agent(段1) → user(排队) → agent(段2, 继续)
                        _items = event_data.get("items", [])
                        _append_ask_records()
                        if full_reply or collected_steps:
                            session_service.add_message(
                                session_id=session_id,
                                role="agent",
                                content=full_reply,
                                agent_name=agent_model.name,
                                metadata={
                                    "steps": collected_steps,
                                    "file_outputs": [],
                                    "token_usage": None,
                                    "run_id": run_id,
                                    "tool_call_count": sum(
                                        1 for _s in collected_steps if _s.get("type") == "tool_call"
                                    ),
                                    "elapsed_seconds": int(time.time() - msg_start_ts),
                                    "model": actual_model,
                                    **version_trace,
                                    "segment": True,
                                },
                            )
                        full_reply = ""
                        collected_steps = []
                        for _it in _items:
                            _atts = _it.get("attachments") or []
                            session_service.add_message(
                                session_id=session_id,
                                role="user",
                                content=_it.get("content", ""),
                                metadata={"attachments": _atts, "queued": True},
                            )
                        await emit({"type": "queued_delivered", "items": _items})
                        # 续播偏移：重连 resume 只回放最后一次投递之后的事件，
                        # 避免已落库段落在前端重复渲染
                        _tinfo_q = task_manager.get(session_id)
                        if _tinfo_q is not None:
                            _tinfo_q.replay_from = len(_tinfo_q.events)

                # 对比快照，找出本次运行期间真正新生成/变化的文件并登记文件中心
                # （统一入口：下方取消/异常分支也用同一函数补检测，停止不丢产出）
                file_outputs = _detect_and_register_outputs(
                    db, user_id, session_id, agent_model.id, output_dir, before_snapshot
                )

                # 服务预览：Agent 在沙箱里起了 Web 服务时，发布成用户可直接访问的临时地址，
                # 源码包（含 HOW_TO_RUN.md）作为一条普通产出并入 file_outputs 一起交付。
                # 必须在文件清理之前跑：打包要读沙箱落到宿主侧的源码目录。
                try:
                    service_previews = await _publish_service_previews(
                        db, user_id, session_id, agent_model.id, output_dir, emit
                    )
                except (Exception, asyncio.CancelledError) as e:
                    logger.warning(f"[WARN] 服务预览发布失败（不影响聊天）: {e}")
                for _pv in service_previews:
                    _bundle = _pv.get("bundle")
                    if _bundle and _bundle not in file_outputs:
                        file_outputs.append(_bundle)

                # 收尾清理：Skill 单次访问注册表（会话文件保留在本地，作为权威存储）
                try:
                    _final_session_cleanup(session_id)
                except Exception as e:
                    logger.warning(f"[WARN] 会话收尾清理失败（不影响聊天）: {e}")

                # ask_user 交互记录：决策问题与用户选择全部写入 steps（刷新后可还原）
                _append_ask_records()

                # 保存AI回复到数据库（含 steps/token/file_outputs，刷新后可完整还原格式）
                # Post-process: append citation mapping from SearchKnowledge tool
                from app.agents.tools.search_knowledge import pop_citation_map
                _cite_map = pop_citation_map(session_id)
                if _cite_map:
                    mapping = ";".join(f"{k}={v}" for k, v in sorted(_cite_map.items()))
                    cite_text = f"\n\n引用映射:{mapping}"
                    full_reply += cite_text
                    await emit({"type": "chunk", "content": cite_text})

                # 无正文文本但有执行轨迹/产出文件时也落库：撞轮数上限等场景下
                # full_reply 为空，不存会导致刷新后整轮工作痕迹丢失
                # 用户停止：AgentScope 吞掉 CancelledError 后优雅收尾，代码照常走到这里，
                # 依据 cancel_requested 标记给落库加上停止标识，收尾事件用 stopped
                _tinfo = task_manager.get(session_id)
                stopped_by_user = bool(_tinfo and _tinfo.cancel_requested)
                # 落库前净化：把模型写给用户的沙箱内部地址换成真实预览地址
                full_reply = _sanitize_reply(full_reply, service_previews)
                if full_reply or collected_steps or file_outputs or service_previews or token_usage:
                    _meta = {
                        "steps": collected_steps,
                        "file_outputs": file_outputs,
                        "service_previews": service_previews,
                        "token_usage": token_usage,
                        "run_id": run_id,
                        "tool_call_count": tool_call_count,
                        "tool_error_count": tool_error_count,
                        "structured_output": structured_output,
                        "elapsed_seconds": int(time.time() - msg_start_ts),
                        "model": actual_model,
                        **version_trace,
                    }
                    if quality_metrics_snapshot:
                        # 质量闭环指标：监控页按此聚合"采集触发率""验证发现错误数"
                        _meta["quality"] = quality_metrics_snapshot
                    if stopped_by_user:
                        _meta["partial"] = True
                        # 谁停的：用户按 Esc（user_stopped）还是管理员吊销会话（admin_revoked）
                        _meta["stop_reason"] = stop_reason_of(session_id)
                    completed_message = session_service.add_message(
                        session_id=session_id,
                        role="agent",
                        content=full_reply,
                        agent_name=agent_model.name,
                        metadata=_meta,
                    )
                    if not stopped_by_user:
                        from app.services.skill_evolution.worker import safe_enqueue
                        evolution_job = safe_enqueue(db, user_id=user_id, session_id=session_id,
                            message_id=completed_message.id, model=actual_model,
                            selected_skills=selected_skills, versions=version_trace.get('skill_versions', {}))
                        if evolution_job:
                            await emit({'type': 'skill_evolution', 'job_id': evolution_job.id,
                                        'status': evolution_job.status})

                # 推送文件下载链接
                for fo in file_outputs:
                    await emit({
                        "type": "file_output",
                        "filename": fo["filename"],
                        "download_url": fo["download_url"],
                        "size": fo["size"],
                    })

                await emit({"type": "stopped" if stopped_by_user else "end"})

            except asyncio.CancelledError:
                # 取消未被 AgentScope 吞掉而传播到这里：落库已产出的部分内容后继续抛出
                logger.info(f"[后台任务] 会话 {session_id} 被用户停止（取消传播）")
                _append_ask_records()
                # 产出文件补检测：中途停止时正常分支没走到检测，已生成的文件
                # 也要登记并可下载（file_outputs 已有值说明正常分支检测过，跳过）
                if not file_outputs:
                    try:
                        file_outputs = _detect_and_register_outputs(
                            db, user_id, session_id, agent_model.id, output_dir, before_snapshot
                        )
                        if file_outputs:
                            _final_session_cleanup(session_id)
                    except Exception as _fe:
                        logger.warning(f"[WARN] 停止后补检测产出文件失败（不影响停止）: {_fe}")
                # 取消分支不发布新预览（用户已明确停止），但正常分支已发布的要一并落库
                full_reply = _sanitize_reply(full_reply, service_previews)
                if full_reply or collected_steps or file_outputs or service_previews or token_usage:
                    try:
                        session_service.add_message(
                            session_id=session_id,
                            role="agent",
                            content=full_reply,
                            agent_name=agent_model.name,
                            metadata={
                                "steps": collected_steps,
                                "file_outputs": file_outputs,
                                "service_previews": service_previews,
                                "token_usage": token_usage,
                                "run_id": run_id,
                                "tool_call_count": tool_call_count,
                                "elapsed_seconds": int(time.time() - msg_start_ts),
                                "partial": True,
                                "stop_reason": stop_reason_of(session_id),
                                "model": actual_model,
                                **version_trace,
                            }
                        )
                    except Exception:
                        pass
                for fo in file_outputs:
                    try:
                        await emit({
                            "type": "file_output",
                            "filename": fo["filename"],
                            "download_url": fo["download_url"],
                            "size": fo["size"],
                        })
                    except (Exception, asyncio.CancelledError):
                        break
                try:
                    await emit({"type": "stopped"})
                except Exception:
                    pass
                raise

            except Exception as e:
                logger.exception(f"[后台任务] 会话 {session_id} 执行失败: {e}")
                # 失败前发生的交互记录同样写入 steps
                _append_ask_records()
                # 产出文件补检测：中途失败时正常分支没走到检测，已生成的文件
                # 也要登记并可下载（file_outputs 已有值说明正常分支检测过，跳过）
                if not file_outputs:
                    try:
                        file_outputs = _detect_and_register_outputs(
                            db, user_id, session_id, agent_model.id, output_dir, before_snapshot
                        )
                        if file_outputs:
                            _final_session_cleanup(session_id)
                    except Exception as _fe:
                        logger.warning(f"[WARN] 失败后补检测产出文件失败（不影响报错）: {_fe}")
                # 服务预览补发布：中途报错时沙箱里的服务可能已经起来了，
                # 与"产出文件补检测"同理，不因主流程失败就丢掉已具备的能力
                if not service_previews:
                    try:
                        service_previews = await _publish_service_previews(
                            db, user_id, session_id, agent_model.id, output_dir, emit
                        )
                        for _pv in service_previews:
                            _bundle = _pv.get("bundle")
                            if _bundle and _bundle not in file_outputs:
                                file_outputs.append(_bundle)
                    except Exception as _pe:
                        logger.warning(f"[WARN] 失败后补发布服务预览失败（不影响报错）: {_pe}")
                # 落库已生成的部分（中途失败不丢已跑出的内容；无正文但有轨迹也存）
                full_reply = _sanitize_reply(full_reply, service_previews)
                if full_reply or collected_steps or file_outputs or service_previews or token_usage:
                    try:
                        session_service.add_message(
                            session_id=session_id,
                            role="agent",
                            content=full_reply,
                            agent_name=agent_model.name,
                            metadata={
                                "steps": collected_steps,
                                "file_outputs": file_outputs,
                                "service_previews": service_previews,
                                "token_usage": token_usage,
                                "run_id": run_id,
                                "tool_call_count": tool_call_count,
                                "elapsed_seconds": int(time.time() - msg_start_ts),
                                "partial": True,
                                "model": actual_model,
                                **version_trace,
                            }
                        )
                    except Exception:
                        pass
                for fo in file_outputs:
                    try:
                        await emit({
                            "type": "file_output",
                            "filename": fo["filename"],
                            "download_url": fo["download_url"],
                            "size": fo["size"],
                        })
                    except Exception:
                        break
                await emit({"type": "error", "content": f"Agent处理失败: {e}"})
        finally:
            current_interaction.reset(cv_token)
            interaction.unregister_channel(session_id)
            current_progress.reset(prog_token)
            # 兜底解除未决决策卡（补发 external_resolved，前端卡片不再残留）
            try:
                await channel.cancel_all()
            except (Exception, asyncio.CancelledError):
                pass
            # 任务结束（正常/异常/取消/超时）统一检查 Agent 残留的等待中工具调用：
            # AgentScope 取消不会清 awaiting 状态，残留会毒化该会话后续所有回复
            try:
                await agent_manager.clear_session_awaiting_state(session_id)
            except (Exception, asyncio.CancelledError):
                pass
            # 上下文落库：进程重启会丢内存 Agent，每轮收尾写 DB，Agent 重建时恢复历史
            try:
                agent_manager.persist_session_context(session_id, db)
            except Exception as e:
                logger.warning(f"[WARN] 会话 {session_id} 上下文落库失败（不影响回复）: {e}")
            db.close()

    return worker


async def _forward_task_events(websocket: WebSocket, info, from_index: int = 0):
    """订阅后台任务事件流并转发给 WebSocket（含从头回放，供断线重连续播）。

    客户端断开时 send_json 抛 WebSocketDisconnect，由外层连接处理器接管收尾——
    后台任务本身不受影响，继续跑完并落库。
    """
    try:
        async for event in info.subscribe(from_index):
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError 是 wsproto/uvicorn 对"连接已关闭"的另一种表达
        raise WebSocketDisconnect()
