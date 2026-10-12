"""Central password policy used by every account/password workflow."""

import re

DEFAULT_INITIAL_PASSWORD = "Initial@123456"
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72  # bcrypt input limit
PASSWORD_CHANGE_REQUIRED_DETAIL = "PASSWORD_CHANGE_REQUIRED:首次登录必须先修改密码"


class PasswordPolicyError(ValueError):
    pass


def validate_password(password: str, *, account: str | None = None,
                      allow_initial_password: bool = False) -> str:
    value = password or ""
    if len(value) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(f"密码至少 {PASSWORD_MIN_LENGTH} 位")
    if len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise PasswordPolicyError("密码过长，请控制在 72 个字节以内")
    if not re.search(r"[A-Z]", value):
        raise PasswordPolicyError("密码必须包含大写字母")
    if not re.search(r"[a-z]", value):
        raise PasswordPolicyError("密码必须包含小写字母")
    if not re.search(r"\d", value):
        raise PasswordPolicyError("密码必须包含数字")
    if not allow_initial_password and value == DEFAULT_INITIAL_PASSWORD:
        raise PasswordPolicyError("新密码不能与初始密码相同")
    normalized_account = (account or "").strip().lower()
    if normalized_account and len(normalized_account) >= 4 and normalized_account in value.lower():
        raise PasswordPolicyError("密码不能包含完整登录账号")
    return value
