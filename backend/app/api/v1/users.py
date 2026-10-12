from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import json
import re
import os
from app.database import get_db
from app.config import UPLOAD_DIR
from app.core.avatars import (
    GLOBAL_AGENT_AVATAR_URL,
    GLOBAL_AGENT_AVATAR_PREFIX,
    global_agent_avatar_path,
    global_agent_avatar_url,
)
from fastapi import UploadFile, File
from fastapi.responses import FileResponse
from app.services.auth_service import AuthService
from app.schemas.user import CreateUserRequest, UserResponse, UpdateUserRequest
from app.core.deps import require_admin, get_current_user, get_current_user_for_password_change
from fastapi import Request
from app.core.security import verify_password, get_password_hash
from app.core.password_policy import (
    DEFAULT_INITIAL_PASSWORD, PasswordPolicyError, validate_password,
)
from datetime import datetime, timezone
from app.core.roles import all_permission_codes
from app.core.deps import require_perm
from app.core.cache import cache_delete_pattern
from app.schemas.destroy import DestroyRequest
from app.models.password_reset import PasswordResetCode
from app.models.file import FileRecord
from app.models.knowledge import KnowledgeBase, KnowledgeDocument
from app.models.mcp_server import McpServer
from app.models.skill_upload_job import SkillUploadJob
from app.core.secrets import obfuscate_secret, reveal_secret
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.models.user_skill_access import UserSkillAccess
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.session import Session as SessionModel
from app.models.message import Message
from app.services.file_service import FileService
from app.services.session_guard import revoke_user_sessions
from app.services.organization_management import GovernanceError, ensure_last_admin, lock_active_admins, validate_agent_ids


'''用户管理：创建、列表、更新、禁用（管理员专用）'''


router = APIRouter(prefix="/users", tags=["用户管理"])


def _perm_scope_snapshot(db: Session, user: User) -> tuple:
    """权限相关字段快照：角色 / 功能权限集合 / 启用状态 / Agent 授权。

    只有这些字段变化才需要把用户踢下线重登（改邮箱、改用户名不打扰人）。
    功能权限按集合比较：库里存的是 JSON 串，顺序差异不代表权限变化，
    直接比字符串会把没变权限的用户也无谓地踢下线。
    """
    from app.core.roles import parse_permissions
    agent_ids = tuple(sorted(
        a.agent_id
        for a in db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user.id).all()
    ))
    return (
        user.role,
        frozenset(parse_permissions(user.permissions)),
        bool(user.is_active),
        agent_ids,
    )


def _fill_skill_grants(resp: UserResponse, db: Session, user_id: int) -> None:
    """回填用户的技能/技能包授权 ID（编辑弹窗回显用；授权实时校验，无需吊销 token）。"""
    rows = db.query(UserSkillAccess).filter(UserSkillAccess.user_id == user_id).all()
    resp.skill_grant_skill_ids = sorted(r.target_id for r in rows if r.target_type == "skill")
    resp.skill_grant_package_ids = sorted(r.target_id for r in rows if r.target_type == "package")


def _replace_skill_grants(db: Session, user_id: int, skill_ids: Optional[List[int]],
                          package_ids: Optional[List[int]]) -> None:
    """整体替换技能授权（None=不修改该维度，[]=清空）。授权覆盖角色门槛与广场隐藏。"""
    from app.services.skill_hub.access import managed_map
    from app.models.skill_hub import SkillHubResource
    personal_ids = set(managed_map(db))
    personal_packages = {r.package_id for r in db.query(SkillHubResource).filter(SkillHubResource.owner_id.isnot(None)) if r.package_id}
    if set(skill_ids or []) & personal_ids or set(package_ids or []) & personal_packages:
        raise HTTPException(400, '个人技能只能由作者定向分享，不能通过系统授权越权开放')
    if skill_ids is not None:
        valid_ids = set()
        if skill_ids:
            rows = db.query(Skill.id).filter(
                Skill.id.in_(skill_ids),
                Skill.is_active == True,  # noqa: E712
                Skill.builtin_dialog == False,  # noqa: E712
                Skill.builtin_task == False,  # noqa: E712
            ).all()
            valid_ids = {r[0] for r in rows}
            invalid = set(skill_ids) - valid_ids
            if invalid:
                raise HTTPException(status_code=400, detail=f"不可用的技能 ID: {sorted(invalid)}")
        db.query(UserSkillAccess).filter(
            UserSkillAccess.user_id == user_id,
            UserSkillAccess.target_type == "skill",
        ).delete(synchronize_session=False)
        for skill_id in sorted(valid_ids):
            db.add(UserSkillAccess(user_id=user_id, target_type="skill", target_id=skill_id))
    if package_ids is not None:
        valid_ids = set()
        if package_ids:
            rows = db.query(SkillPackage.id).filter(
                SkillPackage.id.in_(package_ids),
                SkillPackage.is_active == True,  # noqa: E712
            ).all()
            valid_ids = {r[0] for r in rows}
            invalid = set(package_ids) - valid_ids
            if invalid:
                raise HTTPException(status_code=400, detail=f"不可用的技能包 ID: {sorted(invalid)}")
        db.query(UserSkillAccess).filter(
            UserSkillAccess.user_id == user_id,
            UserSkillAccess.target_type == "package",
        ).delete(synchronize_session=False)
        for package_id in sorted(valid_ids):
            db.add(UserSkillAccess(user_id=user_id, target_type="package", target_id=package_id))


@router.get("/skill-grant-options")
async def get_skill_grant_options(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """技能授权选项（人员编辑弹窗的授权树数据；require_admin 与用户管理口径一致）。"""
    skills = db.query(Skill).filter(
        Skill.is_active == True,  # noqa: E712
        Skill.builtin_dialog == False,  # noqa: E712
        Skill.builtin_task == False,  # noqa: E712
    ).all()
    packages = db.query(SkillPackage).filter(
        SkillPackage.is_active == True,  # noqa: E712
    ).all()
    from app.services.skill_hub.access import managed_map
    personal_ids = set(managed_map(db))
    skills = [s for s in skills if s.id not in personal_ids]
    personal_packages = {s.package_id for s in db.query(Skill).filter(Skill.id.in_(personal_ids)) if s.package_id}
    packages = [p for p in packages if p.id not in personal_packages]
    return {
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "label": s.display_name_zh or s.display_name or s.name,
                "package_id": s.package_id,
                "is_hidden": bool(s.is_hidden),
            }
            for s in skills
        ],
        "packages": [
            {
                "id": p.id,
                "name": p.name,
                "label": p.display_name or p.name,
                "is_hidden": bool(p.is_hidden),
            }
            for p in packages
        ],
    }


@router.post("/", response_model=UserResponse)
async def create_user(
    data: CreateUserRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """创建新用户（仅管理员）"""
    try:
        if data.role not in ("admin", "operator", "viewer"):
            raise HTTPException(status_code=400, detail="未知的基础角色")
        agent_ids = validate_agent_ids(db, data.agent_ids)
        auth_service = AuthService(db)
        new_user = auth_service.create_user(
            account=data.account,
            username=data.username,
            password=data.password,
            email=data.email,
            role=data.role,
            phone=data.phone
        )
        
        # 授予Agent访问权限
        for agent_id in sorted(agent_ids):
            auth_service.grant_agent_access(new_user.id, agent_id)
        
        return new_user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/", response_model=List[UserResponse])
async def get_users(
    sort_by: Optional[str] = None,      # username / created_at / last_login
    sort_order: Optional[str] = None,   # asc / desc
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """获取所有用户列表（仅管理员）"""
    q = db.query(User)
    sort_col = {
        "username": User.username,
        "created_at": User.created_at,
        "last_login": User.last_login,
    }.get(sort_by or "")
    if sort_col is not None:
        q = q.order_by(sort_col.desc() if (sort_order or "asc") == "desc" else sort_col.asc())
    users = q.all()
    # 填充每个用户的 Agent 授权列表，供管理后台展示/编辑
    result = []
    for u in users:
        resp = UserResponse.model_validate(u)
        resp.agent_ids = [
            a.agent_id for a in db.query(UserAgentAccess).filter(UserAgentAccess.user_id == u.id).all()
        ]
        _fill_skill_grants(resp, db, u.id)
        result.append(resp)
    return result


'''个人信息：当前用户自助接口
注意：注册在 /{user_id} 路由之前，否则 /users/me 会被路径参数路由捕获'''


class UpdateMyRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


class ChangeMyPasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.get("/me")
async def get_my_profile(current_user: User = Depends(get_current_user_for_password_change)):
    """个人完整信息（当前用户）"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "account": current_user.account,
        "email": current_user.email,
        "phone": current_user.phone,
        "avatar_url": current_user.avatar_url,
        # Agent 头像全局化：所有人共用管理员设定的一张，URL 固定（与 user_id 无关）
        "agent_avatar_url": global_agent_avatar_url(),
        "role": current_user.role,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at,
        "last_login": current_user.last_login,
        "last_active": current_user.last_active,
        "last_active": current_user.last_active,
        "last_active": current_user.last_active,
        "last_active": current_user.last_active,
        # 邮箱密码（邮件 MCP 用）是否已配置；密码本身不在本接口返回
        "has_mail_password": bool(current_user.mail_password),
        "must_change_password": bool(current_user.must_change_password),
    }


@router.put("/me")
async def update_my_profile(
    data: UpdateMyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """更新个人邮箱/手机号（当前用户）"""
    user = db.query(User).filter(User.id == current_user.id).first()
    if data.email is not None:
        user.email = data.email
    if data.phone is not None:
        if not re.fullmatch(r"\d{11}", data.phone):
            raise HTTPException(status_code=400, detail="手机号必须为11位数字")
        user.phone = data.phone
    db.commit()
    db.refresh(user)
    return {"message": "更新成功", "email": user.email, "phone": user.phone}


class UpdateMailboxRequest(BaseModel):
    mail_password: str


@router.get("/me/mail-password")
async def get_my_mail_password(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查看本人邮箱密码明文（仅用于个人信息页"眼睛"按钮，按需调用）。
    密码永不出现在 /users/me 的返回里。"""
    user = db.query(User).filter(User.id == current_user.id).first()
    response.headers["Cache-Control"] = "no-store"
    return {"mail_password": reveal_secret(user.mail_password) if user else ""}


@router.post("/me/mailbox")
async def update_my_mailbox(
    data: UpdateMailboxRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """保存本人邮箱密码（邮箱平台的独立密码/授权码；不会修改邮箱平台的密码）。
    保存后清空 Agent 缓存：邮件 MCP 子进程在构建 Agent 时注入凭据，需重建才生效。"""
    pwd = (data.mail_password or "").strip()
    if not pwd:
        raise HTTPException(status_code=400, detail="邮箱密码不能为空")
    user = db.query(User).filter(User.id == current_user.id).first()
    user.mail_password = obfuscate_secret(pwd)
    db.commit()
    try:
        from app.agents.manager import agent_manager
        agent_manager.clear_cache()
    except Exception:
        pass
    return {"message": "邮箱密码已更新"}



'''个人头像：上传/读取
存储：uploads/avatars/avatar_{user_id}.{ext}，重复上传覆盖旧文件。
读取接口免鉴权——<img> 标签无法携带 token，内部平台头像不属于敏感数据。'''

AVATAR_DIR = str(UPLOAD_DIR / "avatars")
AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AVATAR_MAX_BYTES = 2 * 1024 * 1024
# 全局 Agent 头像：管理员设定一次，所有人共用。文件名固定（不分用户），
# 读取接口忽略 user_id 始终返回它；URL 也固定，前端 <img> 直接引用
async def _save_avatar_file(user_id: int, prefix: str, file: UploadFile) -> str:
    """头像落盘的公共逻辑：校验大小/格式 -> 覆盖式写入 uploads/avatars/。
    prefix 区分文件名前缀（avatar / agent_avatar），返回实际存储的文件名。"""
    content = await file.read()
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="头像图片不能超过 2MB")
    ext = os.path.splitext(os.path.basename(file.filename or ""))[1].lower()
    if ext not in AVATAR_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPG/WEBP/GIF 图片")

    os.makedirs(AVATAR_DIR, exist_ok=True)
    # 覆盖式保存：先清掉该用户的旧头像（扩展名可能变化）
    for old in os.listdir(AVATAR_DIR):
        if old.startswith(f"{prefix}_{user_id}."):
            os.remove(os.path.join(AVATAR_DIR, old))
    stored_name = f"{prefix}_{user_id}{ext}"
    with open(os.path.join(AVATAR_DIR, stored_name), "wb") as f:
        f.write(content)
    return stored_name


async def _save_global_agent_avatar(file: UploadFile) -> str:
    """全局 Agent 头像落盘：覆盖式写入 agent_avatar_global.{ext}（清掉旧扩展名的同名文件）"""
    content = await file.read()
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="头像图片不能超过 2MB")
    ext = os.path.splitext(os.path.basename(file.filename or ""))[1].lower()
    if ext not in AVATAR_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPG/WEBP/GIF 图片")
    os.makedirs(AVATAR_DIR, exist_ok=True)
    for old in os.listdir(AVATAR_DIR):
        if old.startswith(f"{GLOBAL_AGENT_AVATAR_PREFIX}."):
            os.remove(os.path.join(AVATAR_DIR, old))
    stored_name = f"{GLOBAL_AGENT_AVATAR_PREFIX}{ext}"
    with open(os.path.join(AVATAR_DIR, stored_name), "wb") as f:
        f.write(content)
    return stored_name


@router.post("/me/avatar")
async def upload_my_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传/更换个人头像（限 2MB 内的 png/jpg/webp/gif）"""
    stored_name = await _save_avatar_file(current_user.id, "avatar", file)
    user = db.query(User).filter(User.id == current_user.id).first()
    user.avatar = stored_name
    db.commit()
    db.refresh(user)
    return {"avatar_url": user.avatar_url}


@router.post("/me/agent-avatar")
async def upload_my_agent_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传/更换全局 Agent 头像（仅管理员；限 2MB 内的 png/jpg/webp/gif）。
    设定后所有用户的所有 Agent 共用此头像，非管理员无权修改。"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可设置全局 Agent 头像")
    await _save_global_agent_avatar(file)
    # 以磁盘文件为准下发 URL（带 mtime 缓存戳）；刚保存完文件必然存在
    return {"agent_avatar_url": global_agent_avatar_url() or GLOBAL_AGENT_AVATAR_URL}


@router.get("/avatar/{user_id}")
async def get_user_avatar(user_id: int, db: Session = Depends(get_db)):
    """读取用户头像（免鉴权，供 <img> 直接引用）"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.avatar:
        raise HTTPException(status_code=404, detail="头像不存在")
    filename = os.path.basename(user.avatar)
    path = os.path.realpath(os.path.join(AVATAR_DIR, filename))
    if not path.startswith(os.path.realpath(AVATAR_DIR) + os.sep) or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="头像不存在")
    # no-cache：浏览器每次使用前向服务端再验证（ETag/Last-Modified），
    # 重新上传头像后无需改动 URL 也能立即生效
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/agent-avatar/{user_id}")
async def get_user_agent_avatar(user_id: int, db: Session = Depends(get_db)):
    """读取全局 Agent 头像（免鉴权）。user_id 参数保留以兼容旧 URL，实际忽略，始终返回全局头像。"""
    path = global_agent_avatar_path()
    if not path:
        raise HTTPException(status_code=404, detail="Agent 头像未设置")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.get("/agent-avatar")
async def get_global_agent_avatar():
    """读取全局 Agent 头像（免鉴权，无 user_id 版本，供 /me 返回的固定 URL 使用）"""
    path = global_agent_avatar_path()
    if not path:
        raise HTTPException(status_code=404, detail="Agent 头像未设置")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.post("/me/password")
async def change_my_password(
    data: ChangeMyPasswordRequest,
    current_user: User = Depends(get_current_user_for_password_change),
    db: Session = Depends(get_db)
):
    """修改密码（当前用户，需验证旧密码；成功后吊销全部旧会话）"""
    user = db.query(User).filter(User.id == current_user.id).first()
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    try:
        validate_password(data.new_password, account=user.account)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user.password_hash = get_password_hash(data.new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.now(timezone.utc)
    db.commit()
    await revoke_user_sessions(db, user.id, reason="password_changed")
    return {"message": "密码修改成功，请重新登录", "force_relogin": True}


@router.get("/me/usage")
async def get_my_usage(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """个人用量统计：会话/消息/token/工具调用/文件"""
    uid = current_user.id
    sessions_count = db.query(SessionModel).filter(SessionModel.user_id == uid).count()
    session_ids = [s.id for s in db.query(SessionModel.id).filter(SessionModel.user_id == uid).all()]
    messages_count = 0
    input_tokens = 0
    output_tokens = 0
    tool_calls = 0
    if session_ids:
        messages_count = db.query(Message).filter(Message.session_id.in_(session_ids)).count()
        agent_msgs = db.query(Message).filter(
            Message.session_id.in_(session_ids), Message.role == "agent"
        ).all()
        for m in agent_msgs:
            try:
                meta = json.loads(m.metadata_json) if m.metadata_json else {}
            except Exception:
                meta = {}
            tu = meta.get("token_usage") or {}
            input_tokens += int(tu.get("input_tokens") or 0)
            output_tokens += int(tu.get("output_tokens") or 0)
            tool_calls += int(meta.get("tool_call_count") or 0)
    file_usage = FileService(db).usage(uid)
    return {
        "sessions": sessions_count,
        "messages": messages_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "tool_calls": tool_calls,
        "files_count": file_usage["count"],
        "files_bytes": file_usage["used_bytes"],
        "files_quota_bytes": file_usage["quota_bytes"],
    }

@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    data: UpdateUserRequest,
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """更新用户信息（仅管理员）"""
    if data.role is not None or data.is_active is False:
        lock_active_admins(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if data.role is not None and data.role not in ("admin", "operator", "viewer"):
        raise HTTPException(status_code=400, detail="未知的基础角色")
    try:
        agent_ids = (validate_agent_ids(db, data.agent_ids, existing=(
            row.agent_id for row in db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user_id)
        )) if data.agent_ids is not None else None)
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    previous_role = user.role
    previous_active = bool(user.is_active)

    # 变更前的权限快照：提交后比对，决定是否需要吊销该用户的在线会话
    prev_scope = _perm_scope_snapshot(db, user)
    
    # 防止管理员把自己降级/禁用导致系统失去管理员
    if user.id == current_user.id:
        if data.role is not None and data.role != "admin":
            raise HTTPException(status_code=400, detail="不能降低自己的管理员角色")
        if data.is_active is False:
            raise HTTPException(status_code=400, detail="不能禁用自己的账号")

    try:
        ensure_last_admin(db, user, removing=(
            data.role is not None and data.role != "admin" or data.is_active is False
        ))
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    # 更新字段
    if data.email is not None:
        user.email = data.email
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
    # 更新用户名/账号（各自查重，排除自身）
    if data.username is not None:
        if db.query(User).filter(User.username == data.username, User.id != user_id).first():
            raise HTTPException(status_code=400, detail="用户名已存在")
        user.username = data.username
    if data.account is not None:
        if db.query(User).filter(User.account == data.account, User.id != user_id).first():
            raise HTTPException(status_code=400, detail="账号已存在")
        user.account = data.account

    # 更新 Agent 授权（整体替换：先删后建）
    if data.agent_ids is not None:
        db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user_id).delete()
        for agent_id in sorted(agent_ids):
            db.add(UserAgentAccess(user_id=user_id, agent_id=agent_id, access_level="execute"))

    # Skill 授权整体替换（覆盖角色门槛与广场隐藏；实时校验，无需吊销已签发 token）
    _replace_skill_grants(db, user_id, data.skill_grant_skill_ids, data.skill_grant_package_ids)

    # 更新细粒度功能权限（整体替换：校验权限码后存 JSON；[]=清空，None=不修改）
    if data.permissions is not None:
        invalid = [p for p in data.permissions if p not in all_permission_codes()]
        if invalid:
            raise HTTPException(status_code=400, detail=f"未知权限码: {', '.join(invalid)}")
        user.permissions = json.dumps(list(dict.fromkeys(data.permissions))) if data.permissions else None

    db.flush()
    scope_changed = user.id != current_user.id and _perm_scope_snapshot(db, user) != prev_scope
    user.revision = int(user.revision or 0) + 1
    if scope_changed:
        user.token_version = int(user.token_version or 0) + 1
    db.commit()
    db.refresh(user)

    request.state.audit_changes = {
        "role": {"before": previous_role, "after": user.role},
        "is_active": {"before": previous_active, "after": bool(user.is_active)},
        **({"permissions": "changed"} if data.permissions is not None else {}),
        **({"agent_grants": "changed"} if data.agent_ids is not None else {}),
        **({"skill_grants": "changed"} if (
            data.skill_grant_skill_ids is not None or data.skill_grant_package_ids is not None
        ) else {}),
    }

    # 权限真的变了才吊销：旧 token 立刻失效 + 停掉在跑的 Agent 任务 + 通知在线端跳登录页。
    # 用户重新登录后拿到的就是新权限，不必自己手动退出重登。
    # 改自己的场景已被上面的自我保护逻辑挡住，这里再排除一次以防误踢当前管理员。
    if scope_changed:
        await revoke_user_sessions(db, user.id, already_bumped=True)

    resp = UserResponse.model_validate(user)
    resp.agent_ids = [
        a.agent_id for a in db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user.id).all()
    ]
    _fill_skill_grants(resp, db, user.id)
    return resp

@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """禁用用户（仅管理员，软删除）"""
    lock_active_admins(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能禁用自己的账号")
    try:
        ensure_last_admin(db, user, removing=True)
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    was_active = bool(user.is_active)
    user.is_active = False
    user.revision = int(user.revision or 0) + 1
    db.commit()
    request.state.audit_changes = {"is_active": {"before": was_active, "after": False}}
    if was_active:
        await revoke_user_sessions(db, user.id)
    return {"message": "用户已禁用"}

@router.delete("/{user_id}/hard")
async def hard_delete_user(
    user_id: int,
    data: DestroyRequest,
    current_user: User = Depends(require_perm("user_manage.delete.hard")),
    db: Session = Depends(get_db)
):
    """彻底删除用户（硬删除，需要重新输入管理员密码；逻辑复用 Agent 彻底删除模式）。
    级联删除：消息/会话/Agent授权/密码重置码/文件记录；
    知识库/MCP 服务/技能包上传作业的 created_by 置空（保留资源本身）。数据不可恢复。"""
    # 验证管理员密码（凭据按账号查找，兼容旧用户名）
    admin_user = db.query(User).filter(
        (User.account == data.username) | (User.username == data.username)
    ).first()
    if not admin_user or admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号不是管理员")
    if not verify_password(data.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="密码错误")

    lock_active_admins(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能彻底删除自己")
    try:
        ensure_last_admin(db, user, removing=True)
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    from app.services.skill_hub.lifecycle import detach_user
    detach_user(db, user_id)
    # 级联删除（子表 → 父表）
    session_ids = [
        r.id for r in db.query(SessionModel.id).filter(SessionModel.user_id == user_id).all()
    ]
    if session_ids:
        db.query(Message).filter(Message.session_id.in_(session_ids)).delete(
            synchronize_session=False)
    db.query(SessionModel).filter(SessionModel.user_id == user_id).delete(
        synchronize_session=False)
    db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user_id).delete(
        synchronize_session=False)
    db.query(UserSkillAccess).filter(UserSkillAccess.user_id == user_id).delete(
        synchronize_session=False)
    db.query(PasswordResetCode).filter(PasswordResetCode.user_id == user_id).delete(
        synchronize_session=False)
    db.query(FileRecord).filter(FileRecord.user_id == user_id).delete(
        synchronize_session=False)
    # 该用户创建的资源保留，仅置空 created_by
    db.query(KnowledgeBase).filter(KnowledgeBase.created_by == user_id).update(
        {"created_by": None}, synchronize_session=False)
    db.query(KnowledgeDocument).filter(KnowledgeDocument.created_by == user_id).update(
        {"created_by": None}, synchronize_session=False)
    db.query(McpServer).filter(McpServer.created_by == user_id).update(
        {"created_by": None}, synchronize_session=False)
    # 技能包上传作业保留为运维记录，仅置空 created_by（否则外键会挡住用户彻底删除）
    db.query(SkillUploadJob).filter(SkillUploadJob.created_by == user_id).update(
        {"created_by": None}, synchronize_session=False)
    db.delete(user)
    db.commit()

    # 清理 Agent 缓存与用户相关 Redis 缓存
    try:
        from app.agents.manager import agent_manager
        agent_manager.clear_cache()
    except Exception:
        pass
    cache_delete_pattern("users:*")
    return {"message": "用户已彻底删除，数据不可恢复"}


# 管理员一键重置密码的默认密码（固定值，页面不提供自定义）
DEFAULT_RESET_PASSWORD = DEFAULT_INITIAL_PASSWORD


@router.put("/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    data: DestroyRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """管理员一键把用户密码重置为默认密码（需重新输入管理员账号+密码确认）。
    凭据校验逻辑复用彻底删除模式：按账号/用户名定位管理员并验密。"""
    admin_user = db.query(User).filter(
        (User.account == data.username) | (User.username == data.username)
    ).first()
    if not admin_user or admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号不是管理员")
    if not verify_password(data.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="密码错误")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.password_hash = get_password_hash(DEFAULT_RESET_PASSWORD)
    user.must_change_password = True
    user.password_initialized_at = datetime.now(timezone.utc)
    user.password_changed_at = None
    db.commit()
    await revoke_user_sessions(db, user.id, reason="password_reset_by_admin")
    return {"message": f"密码已重置，该用户下次登录请使用默认密码：{DEFAULT_RESET_PASSWORD}"}
