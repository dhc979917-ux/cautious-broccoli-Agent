"""会话任务停止的唯一实现（用户按 Esc / 管理员强制下线 共用）。

停止语义只写一份，避免 WebSocket 层与管理端各写一套后彼此漂移：
  1) 暂停态（等待外部确认）用中断哨兵让恢复循环干净收尾，运行态直接取消协程；
  2) 清理 AgentScope 残留的 awaiting 工具调用，否则该会话后续每次发送都会报错；
  3) 丢弃服务端排队内容；
  4) 终态事件仍由 worker 自己发（依据 cancel_requested），这里不重复推。

调用方：api/v1/chat.py 的 stop 分支（前端 Esc）、services/session_guard.py（管理端吊销）。
"""
import logging
from typing import List

from app.agents import interaction, pending_input
from app.agents.manager import agent_manager
from app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

# 停止原因：落库 metadata.stop_reason 用它区分"用户自己停"与"被管理员吊销"
REASON_USER_STOP = "user_stopped"
REASON_ADMIN_REVOKE = "admin_revoked"


async def stop_session_task(session_id: int, reason: str = REASON_USER_STOP) -> bool:
    """停止一个会话的运行中任务；返回是否确实停掉了任务。

    无运行中任务时同样清一次排队内容：前端在没有任务时按 Esc 也会调这里
    （用来清残留的决策卡等本地状态），服务端保持同样口径。
    """
    info = task_manager.get(session_id)
    if info is None or info.status != "running":
        pending_input.drain(session_id)
        return False

    info.stop_reason = reason
    channel = interaction.get_channel(session_id)
    if channel is not None and channel.has_pending():
        # 暂停态：不直接取消协程，用中断哨兵让恢复循环以 UserInterruptEvent 收尾
        info.cancel_requested = True
        await channel.interrupt_all()
        await task_manager.wait_finished(session_id)
    else:
        await task_manager.cancel(session_id)

    # AgentScope 取消时不会清除 awaiting 状态，残留会让会话后续每次发送直接报错
    try:
        await agent_manager.clear_session_awaiting_state(session_id)
    except Exception as e:
        logger.warning(f"[task_control] 会话 {session_id} 停止后清理 Agent 状态失败: {e}")

    pending_input.drain(session_id)
    return True


async def stop_user_tasks(user_id: int, reason: str = REASON_ADMIN_REVOKE) -> List[int]:
    """停掉某用户名下所有在跑的会话任务，返回被停掉的 session_id 列表。"""
    stopped: List[int] = []
    for sid in task_manager.running_session_ids(user_id):
        try:
            if await stop_session_task(sid, reason):
                stopped.append(sid)
        except Exception as e:
            logger.warning(f"[task_control] 停止会话 {sid} 失败（用户 {user_id}）: {e}")
    if stopped:
        logger.info(f"[task_control] 用户 {user_id} 强制停止会话 {stopped}（{reason}）")
    return stopped


def stop_reason_of(session_id: int) -> str:
    """本次停止的原因（落库 metadata.stop_reason 用）；未记录时按用户主动停止处理。"""
    info = task_manager.get(session_id)
    return (info.stop_reason if info is not None and info.stop_reason else REASON_USER_STOP)
