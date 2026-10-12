from pydantic import BaseModel


class DestroyRequest(BaseModel):
    """彻底删除时的密码验证请求"""
    username: str   # 管理员用户名
    password: str   # 管理员密码（明文，后端立即验证后丢弃）
