from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from pydantic import ConfigDict, field_validator

from app.core.roles import parse_permissions


'''用户数据格式'''


# 创建用户请求（管理员用）
class CreateUserRequest(BaseModel):
    account: str
    username: str
    password: str
    phone: str
    email: Optional[str] = None
    role: str = "operator"     # 默认是 operator。管理员创建用户时可以指定为 admin 或 viewer
    agent_ids: List[int] = []  # 授权可以访问的Agent ID列表

# 用户响应
#  UserInfo 面向登录者本人，需要权限信息（"我能用哪些 Agent"）
#   - UserResponse 面向管理员查看，需要账号管理信息（"这个账号启用了吗？什么时候注册的？"）
#   - 两个都不包含 password_hash——密码哈希永远不能出现在 API 响应中
class UserResponse(BaseModel):
    id: int
    username: str
    account: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None
    last_active: Optional[datetime] = None
    must_change_password: bool = False
    agent_ids: List[int] = []  # 该用户已授权的 Agent ID 列表（路由层填充，非数据库字段）
    # 已授权的技能/技能包 ID 列表（路由层填充；授权覆盖角色门槛与广场隐藏）
    skill_grant_skill_ids: List[int] = []
    skill_grant_package_ids: List[int] = []
    permissions: List[str] = []  # 细粒度功能权限码列表（如 ["mcp_manage"]）

    @field_validator("permissions", mode="before")
    @classmethod
    def _parse_permissions(cls, v):
        """数据库里 permissions 存的是 JSON 字符串，序列化前统一用 roles.parse_permissions 解析"""
        return parse_permissions(v)

    model_config = ConfigDict(from_attributes=True)

# 更新用户请求
# 所有字段都是 Optional（"部分更新"）
# 没有 password 字段。这个接口不能用来修改密码。修改密码需要单独的接口（当前代码里没有实现，是预留功能）
class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    agent_ids: Optional[List[int]] = None
    permissions: Optional[List[str]] = None  # 细粒度功能权限（整体替换；None=不修改，[]=清空）
    username: Optional[str] = None  # 用户名（真实姓名），修改时查重
    account: Optional[str] = None   # 登录账号，修改时查重
    # Skill 授权（整体替换；None=不修改，[]=清空）。包授权覆盖包内全部成员技能
    skill_grant_skill_ids: Optional[List[int]] = None
    skill_grant_package_ids: Optional[List[int]] = None
