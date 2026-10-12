from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime

class SkillResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str] = None
    required_role: str
    is_active: int
    # 内置标志：1=内置技能（对用户端隐藏，仅管理员技能管理可见）
    builtin_dialog: int = 0
    builtin_task: int = 0
    # 中文展示信息（可能为空，前端为空时回退原文）
    display_name_zh: Optional[str] = None
    description_zh: Optional[str] = None
    # 所属技能包（NULL=独立技能）
    package_id: Optional[int] = None
    # 磁盘身份（相对 app/agents/ 的路径）与目录缺失标记：
    # dir_missing=True 表示库里有记录但磁盘目录不在，管理端据此显示"目录缺失"并禁用恢复
    rel_path: Optional[str] = None
    dir_missing: bool = False
    # 归属是否被管理员锁定（锁定后同步不再按目录位置改写内置标志）
    scope_locked: bool = False
    # 广场/聊天可见性：True=对未授权非管理员隐藏
    is_hidden: bool = False
    allow_fork: bool = False
    fork_policy_inherited: bool = False
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class CreateSkillRequest(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = None
    required_role: str = "viewer"
    # 创建时即可隐藏（默认公开）；仅影响广场/聊天可见性，不影响授权用户
    is_hidden: bool = False

class UpdateSkillRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    required_role: Optional[str] = None
    is_hidden: Optional[bool] = None
    allow_fork: Optional[bool] = None


class ScopeChangeRequest(BaseModel):
    """技能归属切换请求：optional=可选技能，dialog=工具助手内置，task=数据分析师内置。

    取值与校验在 skill_service.ROOT_BY_SCOPE，schema 这里只声明字段，
    避免"合法取值"在两个地方各写一份。
    """
    scope: str


# 技能包响应（管理端包列表 / 前端分组）
class SkillPackageResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str] = None
    required_role: Optional[str] = None  # 包级权限；None=未设置（成员技能各自独立）
    is_active: int
    rel_path: Optional[str] = None
    dir_missing: bool = False
    is_hidden: bool = False
    allow_fork: bool = False
    fork_policy_inherited: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UpdatePackageRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    # 包级权限：None=不修改；""=清除（成员恢复各自独立）；viewer/operator/admin=设置并让成员统一沿用
    required_role: Optional[str] = None
    is_hidden: Optional[bool] = None
    allow_fork: Optional[bool] = None
