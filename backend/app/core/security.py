from datetime import datetime, timedelta
from datetime import timezone
from zoneinfo import ZoneInfo
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.config import settings


'''JWT令牌，密码加密'''


# Asia/Shanghai timezone constant
_CST = ZoneInfo("Asia/Shanghai")





# 密码加密上下文
# schemes=["bcrypt"]：指定使用 bcrypt 算法
# deprecated="auto"：如果将来把 schemes 改成 ["argon2", "bcrypt"]，旧的 bcrypt 密码在用户下次登录时会自动升级为 argon2
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证明文密码和哈希密码是否匹配
        - 输入：用户登录时提交的明文密码 + 数据库里存的哈希值
        - 输出：True（匹配）或 False（不匹配）
        - 内部原理：bcrypt 从哈希值中提取盐值，用同一个盐值对明文密码重新哈希，然后比较结果
        - 在 auth_service.py 的 authenticate() 方法里被调用
    """
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """
    将密码哈希加密
        - 输入：明文密码
        - 输出：bcrypt 哈希字符串
        - 在两个地方被调用：
            - init_db.py：创建管理员账号时，get_password_hash("admin123")
            - auth_service.py 的 create_user()：创建新用户时
    """
    return pwd_context.hash(password)

def compute_token_expiry() -> datetime:
    """Compute token expiry with force-relogin window logic.

    Base expiry = now + JWT_EXPIRY_BASE_HOURS. If that moment falls
    in working hours [FORCE_RELOGIN_WINDOW_END, FORCE_RELOGIN_WINDOW_START),
    push it to FORCE_RELOGIN_WINDOW_START:00 that day. Otherwise keep as-is.
    Returns a timezone-aware UTC datetime for JWT exp.
    """
    now_cst = datetime.now(_CST)
    base = now_cst + timedelta(hours=settings.JWT_EXPIRY_BASE_HOURS)
    w_end = settings.FORCE_RELOGIN_WINDOW_END
    w_start = settings.FORCE_RELOGIN_WINDOW_START
    if w_end <= base.hour < w_start:
        expiry_cst = base.replace(hour=w_start, minute=0, second=0, microsecond=0)
    else:
        expiry_cst = base
    return expiry_cst.astimezone(timezone.utc)


def compute_expires_in_seconds() -> int:
    """Seconds from now until the token expires (for login response)."""
    expiry = compute_token_expiry()
    return int((expiry - datetime.now(timezone.utc)).total_seconds())


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建JWT访问令牌"""

    # 第一步：复制传入的数据字典（不修改原始数据）
    to_encode = data.copy()

    # 第二步：计算过期时间
    #   - 如果调用者传了自定义的 expires_delta（比如 30 分钟），就用它
    #   - 否则使用配置文件里的 JWT_ACCESS_TOKEN_EXPIRE_MINUTES（默认 1440 分钟 = 24 小时）
    #   - 把 exp（expire 的缩写）字段加入数据中。这是 JWT 的标准字段，所有 JWT 库都认它
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = compute_token_expiry()
    to_encode.update({"exp": expire})

    # 第三步：用密钥签名，生成最终的令牌字符串
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def decode_token(token: str) -> Optional[dict]:
    """解码JWT令牌，验证是否有效"""
    # jwt.decode() 做了两件事：
    #   1. 验证签名：用同一个密钥重新计算签名，和令牌里的签名对比。如果不一致（说明数据被篡改过），抛出 JWTError
    #   2. 检查过期时间：如果当前时间超过了 exp 字段，也会抛出 JWTError
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None
