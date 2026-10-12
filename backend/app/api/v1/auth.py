from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional, List
import re
from datetime import datetime, timedelta
import hashlib
import secrets
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.services.auth_service import AuthService
from app.schemas.auth import LoginRequest, LoginResponse
from app.core.deps import get_current_user_for_password_change, require_admin
from app.core.security import get_password_hash
from app.core.password_policy import (
    DEFAULT_INITIAL_PASSWORD, PasswordPolicyError, validate_password,
)
from app.services.session_guard import revoke_user_sessions
from app.core.mailer import send_mail
from app.models.user import User
from app.models.password_reset import PasswordResetCode
from app.core.avatars import global_agent_avatar_url
from app.core.cache import cache_delete, cache_get, cache_incr
from app.services import reset_codes
from app.services.organization_access import navigation_permissions


'''登录、获取当前用户信息'''


# 注册请求模型
# 注意：不接受 role 字段！自助注册的角色由服务端配置决定，
# 防止任何人通过传 {"role": "admin"} 提权。角色调整由管理员在后台进行。
class RegisterRequest(BaseModel):
    account: str
    username: str
    password: str
    phone: str
    email: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v):
        if not re.fullmatch(r"\d{11}", v or ""):
            raise ValueError("手机号必须为11位数字")
        return v

router = APIRouter(prefix="/auth", tags=["认证"])

# 登录防暴力破解（Redis 可用时生效）：同账号连续失败上限与锁定时长
LOGIN_FAIL_LIMIT = 5
LOGIN_LOCK_SECONDS = 300

@router.post("/login", response_model=LoginResponse)
async def login(login_data: LoginRequest, db: Session = Depends(get_db)):
    """用户登录接口"""
    # 防暴力破解（Redis 可用时才生效）：同账号连续失败 5 次锁定 5 分钟
    fail_key = f"login_fail:{login_data.account.strip().lower()}"
    fails = cache_get(fail_key)
    if fails is not None and int(fails) >= LOGIN_FAIL_LIMIT:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请 5 分钟后再试")
    try:
        auth_service = AuthService(db)
        result = auth_service.authenticate(login_data)
        cache_delete(fail_key)  # 登录成功，清除失败计数
        return result
    except ValueError as e:
        cache_incr(fail_key, ttl=LOGIN_LOCK_SECONDS)
        raise HTTPException(status_code=401, detail=str(e))

@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_user_for_password_change),
                                db: Session = Depends(get_db)):
    """获取当前登录用户信息"""
    # /me 接口通过 Depends(get_current_user) 自动从 JWT 中解析出当前用户并注入到函数里，函数直接返回这个用户的常用信息，无需前端传递任何用户标识
    return {
        "id": current_user.id,
        "username": current_user.username,
        "account": current_user.account,
        "email": current_user.email,
        "phone": current_user.phone,
        "avatar_url": current_user.avatar_url,
        "agent_avatar_url": global_agent_avatar_url(),
        "role": current_user.role,
        "agent_permissions": current_user.agent_permissions,
        "permissions": navigation_permissions(db, current_user),
        "must_change_password": bool(current_user.must_change_password),
    }

@router.post("/register")
async def register(data: RegisterRequest, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """创建新用户（仅管理员）：登录页已取消自助注册，账号一律由管理员在后台创建"""
    # 检查账号是否已存在
    if db.query(User).filter(User.account == data.account).first():
        raise HTTPException(status_code=400, detail="账号已存在")
    # 检查用户名（真实姓名）是否已存在
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    # 创建用户：角色由服务端配置决定（viewer/operator，永不接受客户端指定）
    from app.core.security import get_password_hash
    from app.core.roles import SELF_REGISTER_ROLES
    from app.config import settings
    role = settings.REGISTER_DEFAULT_ROLE
    if role not in SELF_REGISTER_ROLES:
        role = "viewer"
    user = User(
        account=data.account,
        username=data.username,
        password_hash=get_password_hash(DEFAULT_INITIAL_PASSWORD),
        email=data.email,
        phone=data.phone,
        role=role,
        is_active=True,
        must_change_password=True,
        password_initialized_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return {
        "message": "注册成功",
        "user": {
            "id": user.id,
            "username": user.username,
            "account": user.account,
            "email": user.email,
            "phone": user.phone,
            "role": user.role
        }
    }


# ===== 忘记密码：邮箱验证码重置 =====


class SendResetCodeRequest(BaseModel):
    username: str  # 登录账号（account）
    email: str


class ResetPasswordRequest(BaseModel):
    username: str  # 登录账号（account）
    email: str
    code: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_pwd(cls, v):
        try:
            return validate_password(v)
        except PasswordPolicyError as exc:
            raise ValueError(str(exc)) from exc


# 统一报错文案：用户名不存在 / 邮箱不匹配 / 未绑邮箱 / 账号禁用，都报同一句（不泄露具体哪一项错）
_RESET_MATCH_ERR = "邮箱或用户名错误"
_RESET_SENT = {"message": "验证码已发送，请注意查收（5 分钟内有效）"}


def _find_reset_user(db: Session, username: str, email: str):
    """按登录账号（account）+ 邮箱定位用户。
    用户名不存在 / 账号禁用 / 未绑邮箱 / 邮箱不一致均返回 None，由调用方统一报 _RESET_MATCH_ERR。"""
    uname = (username or "").strip().lower()
    mail = (email or "").strip().lower()
    if not uname or not mail:
        return None
    user = db.query(User).filter(func.lower(User.account) == uname).first()
    if not user or not user.is_active:
        return None
    if not user.email or user.email.strip().lower() != mail:
        return None
    return user


@router.post("/password/send-code")
async def send_password_reset_code(data: SendResetCodeRequest, db: Session = Depends(get_db)):
    """忘记密码第 1 步：校验登录账号 + 绑定邮箱后发送验证码（同一用户 60 秒冷却、每日最多 5 封）"""
    user = _find_reset_user(db, data.username, data.email)
    if user is None:
        raise HTTPException(status_code=400, detail=_RESET_MATCH_ERR)
    code = f"{secrets.randbelow(1000000):06d}"
    if reset_codes.available():
        # Redis 路径：冷却/每日上限/码记录全部走 TTL，不落 DB
        if not reset_codes.send_allowed(user.id):
            return _RESET_SENT
        reset_codes.store_code(user.id, hashlib.sha256(code.encode()).hexdigest())
    else:
        # 回退路径：无 Redis 时沿用原 DB 逻辑
        now = datetime.utcnow()
        last = (
            db.query(PasswordResetCode)
            .filter(PasswordResetCode.user_id == user.id)
            .order_by(PasswordResetCode.created_at.desc())
            .first()
        )
        if last and last.created_at and (now - last.created_at).total_seconds() < 60:
            return _RESET_SENT  # 60 秒冷却：静默忽略
        sent_today = (
            db.query(PasswordResetCode)
            .filter(
                PasswordResetCode.user_id == user.id,
                PasswordResetCode.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0),
            )
            .count()
        )
        if sent_today >= 5:
            return _RESET_SENT  # 每日上限：静默忽略
        db.add(PasswordResetCode(
            user_id=user.id,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=now + timedelta(minutes=5),
        ))
        db.commit()
    send_mail(
        user.email,
        "TaskPilot 密码重置验证码",
        f"您正在重置 TaskPilot 账号密码。\n验证码：{code}（5 分钟内有效，仅可使用一次）。\n如非本人操作，请忽略本邮件。",
    )
    return _RESET_SENT


@router.post("/password/reset")
async def reset_password(data: ResetPasswordRequest, db: Session = Depends(get_db)):
    """忘记密码第 2 步：校验登录账号 + 邮箱后校验验证码并重置密码（码 5 分钟过期、最多尝试 5 次、一次性）"""
    user = _find_reset_user(db, data.username, data.email)
    if user is None:
        raise HTTPException(status_code=400, detail=_RESET_MATCH_ERR)
    if reset_codes.available():
        # Redis 路径：过期由 TTL 自动处理
        rec = reset_codes.get_record(user.id)
        if not rec:
            raise HTTPException(status_code=400, detail="请先获取验证码")
        if int(rec.get("att", 0)) >= reset_codes.MAX_ATTEMPTS:
            raise HTTPException(status_code=400, detail="验证码错误次数过多，请重新发送")
        if hashlib.sha256((data.code or "").encode()).hexdigest() != rec.get("h"):
            reset_codes.bump_attempts(user.id)
            raise HTTPException(status_code=400, detail="邮箱或验证码不正确")
        reset_codes.consume(user.id)
        user.password_hash = get_password_hash(data.new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.utcnow()
        db.commit()
        await revoke_user_sessions(db, user.id, reason="password_reset")
        return {"message": "密码已重置，请使用新密码登录"}
    # 回退路径：无 Redis 时沿用原 DB 逻辑
    now = datetime.utcnow()
    record = (
        db.query(PasswordResetCode)
        .filter(PasswordResetCode.user_id == user.id)
        .order_by(PasswordResetCode.created_at.desc())
        .first()
    )
    if not record or record.used:
        raise HTTPException(status_code=400, detail="请先获取验证码")
    if record.expires_at <= now:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新发送")
    if record.attempts >= 5:
        raise HTTPException(status_code=400, detail="验证码错误次数过多，请重新发送")
    if hashlib.sha256((data.code or "").encode()).hexdigest() != record.code_hash:
        record.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="邮箱或验证码不正确")
    user.password_hash = get_password_hash(data.new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.utcnow()
    record.used = True
    db.commit()
    await revoke_user_sessions(db, user.id, reason="password_reset")
    return {"message": "密码已重置，请使用新密码登录"}
