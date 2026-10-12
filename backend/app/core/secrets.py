"""低敏感密钥的可逆混淆存取（邮箱密码等）。

注意：这是混淆（obfuscation），不是加密——防止数据库文件被随手看到明文，
不能抵御拿到代码与配置的访问者。需要加密级安全时换成 Fernet 对称加密。
混淆密钥从 JWT_SECRET_KEY 派生，部署换密钥会导致旧值解不出（返回空串）。"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from app.config import settings

_PREFIX = "obf$"
_ENCRYPTED_PREFIX = "enc$v2$"


def _key_bytes() -> bytes:
    return hashlib.sha256((settings.LEGACY_SECRET_ENCRYPTION_KEY or settings.JWT_SECRET_KEY or "taskpilot").encode("utf-8")).digest()


def obfuscate_secret(plain: str) -> str:
    """明文 -> 混淆串（带前缀，便于区分历史明文数据）"""
    if plain is None:
        return None
    if settings.SECRET_ENCRYPTION_KEY:
        key = hashlib.sha256(settings.SECRET_ENCRYPTION_KEY.encode("utf-8")).digest()
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), _ENCRYPTED_PREFIX.encode("ascii"))
        return _ENCRYPTED_PREFIX + base64.b64encode(nonce + encrypted).decode("ascii")
    data = plain.encode("utf-8")
    key = _key_bytes()
    x = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return _PREFIX + base64.b64encode(x).decode("ascii")


def reveal_secret(stored) -> str:
    """混淆串 -> 明文；无前缀视为历史明文直接返回；解析失败返回空串"""
    if not stored:
        return ""
    if stored.startswith(_ENCRYPTED_PREFIX):
        if not settings.SECRET_ENCRYPTION_KEY:
            return ""
        try:
            payload = base64.b64decode(stored[len(_ENCRYPTED_PREFIX):], validate=True)
            key = hashlib.sha256(settings.SECRET_ENCRYPTION_KEY.encode("utf-8")).digest()
            return AESGCM(key).decrypt(payload[:12], payload[12:], _ENCRYPTED_PREFIX.encode("ascii")).decode("utf-8")
        except (ValueError, TypeError, InvalidTag):
            return ""
    if not stored.startswith(_PREFIX):
        return stored
    try:
        x = base64.b64decode(stored[len(_PREFIX):].encode("ascii"))
        key = _key_bytes()
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(x)).decode("utf-8")
    except Exception:
        return ""
