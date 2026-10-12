from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey

from app.database import Base


'''忘记密码验证码记录：码本身以 sha256 哈希落库（不存明文）。
每条记录 5 分钟过期、最多校验 5 次；发送侧另有 60 秒冷却与每日上限（见 auth 路由）。'''


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # sha256(code) 十六进制，64 位定长
    code_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
