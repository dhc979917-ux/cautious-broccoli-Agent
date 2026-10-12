import os
from typing import Optional

from app.config import UPLOAD_DIR

'''全局 Agent 头像：以磁盘文件为唯一事实来源（不落库）。
存放于 uploads/avatars/agent_avatar_global.{ext}；读取 URL 固定，
下发给前端时附文件 mtime 作为缓存戳，换图后浏览器自动拿新图。
放在 core 下供 api(users/auth) 与 services(auth_service) 共用，避免循环导入。'''

AVATAR_DIR = str(UPLOAD_DIR / "avatars")
GLOBAL_AGENT_AVATAR_URL = "/api/v1/users/agent-avatar"
GLOBAL_AGENT_AVATAR_PREFIX = "agent_avatar_global"


def global_agent_avatar_path() -> Optional[str]:
    """返回全局 Agent 头像的磁盘路径，未设置返回 None。
    优先全局文件；找不到时回退到迁移前按用户存的 agent_avatar_{uid}.*（管理员旧头像），
    保证历史设置在未重新上传前也能继续显示，不会因升级而丢头像。"""
    if not os.path.isdir(AVATAR_DIR):
        return None
    base = os.path.realpath(AVATAR_DIR) + os.sep
    # 1) 全局文件（新机制）
    for name in os.listdir(AVATAR_DIR):
        if name.startswith(f"{GLOBAL_AGENT_AVATAR_PREFIX}."):
            p = os.path.realpath(os.path.join(AVATAR_DIR, name))
            if p.startswith(base) and os.path.exists(p):
                return p
    # 2) 回退：旧的按用户存的 agent_avatar（迁移前管理员设的，取第一个存在的）
    for name in sorted(os.listdir(AVATAR_DIR)):
        if name.startswith("agent_avatar_") and not name.startswith(f"{GLOBAL_AGENT_AVATAR_PREFIX}"):
            p = os.path.realpath(os.path.join(AVATAR_DIR, name))
            if p.startswith(base) and os.path.exists(p):
                return p
    return None


def global_agent_avatar_url() -> Optional[str]:
    """下发给前端的 URL：文件存在才返回（否则 None，前端显示默认图标），
    并附 mtime 缓存戳，防止浏览器缓存旧头像。"""
    p = global_agent_avatar_path()
    if not p:
        return None
    try:
        stamp = int(os.path.getmtime(p))
    except OSError:
        return GLOBAL_AGENT_AVATAR_URL
    return f"{GLOBAL_AGENT_AVATAR_URL}?v={stamp}"
