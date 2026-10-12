"""在线连接登记（按用户维度）。

只回答"这个用户现在有哪些活跃 WebSocket"，不关心推什么内容：调用方给一个
JSON 可序列化的 payload，这里负责逐个投递并清理死连接。

后端固定单 worker 运行（见 backend/Dockerfile 注释：agents_cache 是进程内状态），
所以进程内字典就是全部真相；将来若改多 worker，只需把本模块换成 Redis pub/sub，
调用方（services/session_guard.py）不用改。
"""
import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# {user_id: {websocket, ...}}
_CONNECTIONS: Dict[int, Set] = {}


def register(user_id: Optional[int], ws) -> None:
    """登记一条在线连接（幂等：同一连接重复登记只算一次）。"""
    if user_id is None or ws is None:
        return
    _CONNECTIONS.setdefault(int(user_id), set()).add(ws)


def unregister(user_id: Optional[int], ws) -> None:
    """注销连接；用户最后一条连接断开时清掉整个键，避免字典无限增长。"""
    if user_id is None:
        return
    conns = _CONNECTIONS.get(int(user_id))
    if not conns:
        return
    conns.discard(ws)
    if not conns:
        _CONNECTIONS.pop(int(user_id), None)


def is_online(user_id: Optional[int]) -> bool:
    return bool(user_id is not None and _CONNECTIONS.get(int(user_id)))


async def push(user_id: int, payload: dict,
               close_code: Optional[int] = None, close_reason: str = "") -> int:
    """把 payload 推给该用户的全部在线连接，返回成功投递数。

    close_code 非空时投递后主动关闭连接：服务端踢人不能只依赖前端处理 payload，
    连接断开本身也会触发前端的下线流程（双保险）。
    单条连接失败只丢弃该连接，不影响同用户的其它连接。
    """
    delivered = 0
    for ws in list(_CONNECTIONS.get(int(user_id)) or ()):
        try:
            await ws.send_json(payload)
            delivered += 1
        except Exception as e:
            logger.info(f"[presence] 用户 {user_id} 的一条连接已失效，丢弃: {e}")
            unregister(user_id, ws)
            continue
        if close_code is not None:
            try:
                await ws.close(code=close_code, reason=close_reason)
            except Exception:
                pass
            unregister(user_id, ws)
    return delivered
