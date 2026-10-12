from pydantic import BaseModel
from typing import Optional, List
from pydantic import ConfigDict, field_validator

from app.core.roles import parse_permissions


'''登录相关数据格式'''


# 登录请求（用账号 account + 密码登录）
# 自动校验规则：
#   - account 没传 → FastAPI 自动返回 422 Unprocessable Entity，附带错误信息 "field required"
#   - account 传了数字 123 → Pydantic 会尝试转换成字符串 "123"（Pydantic v2 的默认行为）
#   - account 传了 null → 校验失败，因为不是 Optional
# 前端多传了字段：如"remember_me": true，Pydantic v2 默认会忽略多余字段，不会报错。remember_me 会被静默丢弃
class LoginRequest(BaseModel):
    account: str
    password: str

# 登录响应（后端 → 前端）
class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    # "UserInfo" 用了引号（前向引用），因为 UserInfo 定义在 LoginResponse 后面。Python 要求先定义后使用，用引号包裹可以绕过这个限制
    user: "UserInfo"

# 登录成功后，嵌在 LoginResponse 里返回的用户信息
class UserInfo(BaseModel):
    id: int
    username: str
    account: str = ""
    email: Optional[str] = None # 可选字段，默认值是 None（看用户在数据库中是否传入）
    phone: Optional[str] = None
    avatar_url: Optional[str] = None  # 头像地址（未设置时为 None，前端显示默认图标）
    agent_avatar_url: Optional[str] = None  # 全局 Agent 头像（以磁盘文件为准，未设置时为 None，前端显示默认机器人图标）
    role: str
    agent_permissions: List[str] = []
    permissions: List[str] = []  # 细粒度功能权限码（登录/刷新时下发给前端做菜单与按钮门控）
    must_change_password: bool = False

    @field_validator("permissions", mode="before")
    @classmethod
    def _parse_permissions(cls, v):
        """兼容 ORM 对象（数据库 JSON 字符串）与显式构造（列表）两种来源"""
        return parse_permissions(v)

    #  Pydantic 会自动把 ORM 对象的 .id、.username、.email、.role 属性提取出来。不匹配的字段（如password_hash）会被自动忽略
    model_config = ConfigDict(from_attributes=True)

# Token响应
# 预留的 Schema，可能用于未来的"刷新令牌"接口（只返回新令牌，不返回用户信息）
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
