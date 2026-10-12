"""管理员改动用户权限/角色后的会话吊销。

一处收口三件事，顺序不能换：
  1) token_version +1 并立即提交——旧 JWT 从此在所有校验点（REST 依赖注入、
     WebSocket 握手）都失效，不必等它自然过期；
  2) 停掉该用户在跑的 Agent 任务（等价用户自己按 Esc，落库标记为 admin_revoked）；
  3) 通知在线端下线：推 force_logout 并关闭其 WebSocket。

不含任何 HTTP 概念，便于接口层调用与脚本复用。
"""
import logging

from sqlalchemy.orm import Session

from app.core import presence
from app.models.user import User
from app.services.task_control import REASON_ADMIN_REVOKE, stop_user_tasks

logger = logging.getLogger(__name__)

# 推给前端的事件类型与关闭码：前端据此清本地登录态并跳登录页
FORCE_LOGOUT_EVENT = "force_logout"
CLOSE_CODE_REVOKED = 4001


def bump_token_version(db: Session, user_id: int) -> int:
    """让该用户已签发的全部 token 立刻失效，返回新版本号（用户不存在返回 0）。"""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return 0
    user.token_version = int(user.token_version or 0) + 1
    db.commit()
    return int(user.token_version)


async def revoke_user_sessions(db: Session, user_id: int,
                               reason: str = "permissions_changed",
                               already_bumped: bool = False) -> dict:
    """吊销会话 + 停任务 + 通知在线端下线，返回各步骤结果便于排查。"""
    token_version = (db.query(User.token_version).filter(User.id == user_id).scalar()
                     if already_bumped else bump_token_version(db, user_id))
    stopped = await stop_user_tasks(user_id, REASON_ADMIN_REVOKE)
    notified = await presence.push(
        user_id,
        {"type": FORCE_LOGOUT_EVENT, "reason": reason},
        close_code=CLOSE_CODE_REVOKED,
        close_reason="登录状态已失效",
    )
    logger.info(
        f"[session_guard] 用户 {user_id} 会话已吊销: token_version={token_version}, "
        f"停止会话={stopped}, 通知连接数={notified}"
    )
    return {
        "token_version": token_version,
        "stopped_sessions": stopped,
        "notified": notified,
    }
