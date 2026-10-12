"""Plain-Python regression for the initial-password and forced-change contract."""
import asyncio
import os
import sys

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-at-least-thirty-two-characters"
os.environ["ENV_FILE"] = "__missing_test_env__"
sys.path.insert(0, "backend")

from app.core.password_policy import (  # noqa: E402
    DEFAULT_INITIAL_PASSWORD, PasswordPolicyError, validate_password,
)
from app.core.security import verify_password  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app.schemas.auth import LoginRequest  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402


def expect_invalid(value: str, account: str | None = None) -> None:
    try:
        validate_password(value, account=account)
    except PasswordPolicyError:
        return
    raise AssertionError(f"password unexpectedly accepted: {value!r}")


def main() -> None:
    validate_password("StrongPass2026")
    for value in ("short1A", "lowercase1", "UPPERCASE1", "NoDigitsHere", DEFAULT_INITIAL_PASSWORD):
        expect_invalid(value)
    expect_invalid("UserName2026A", account="username")

    init_db()
    db = SessionLocal()
    try:
        service = AuthService(db)
        user = service.create_user(
            account="policy_user", username="Policy User", password="IgnoredPass2026",
            phone="18888888888", role="viewer",
        )
        assert user.must_change_password is True
        assert verify_password(DEFAULT_INITIAL_PASSWORD, user.password_hash)
        assert not verify_password("IgnoredPass2026", user.password_hash)
        result = service.authenticate(LoginRequest(account="policy_user", password=DEFAULT_INITIAL_PASSWORD))
        assert result.user.must_change_password is True
        try:
            asyncio.run(get_current_user(user))
        except HTTPException as exc:
            assert exc.status_code == 403 and "PASSWORD_CHANGE_REQUIRED" in str(exc.detail)
        else:
            raise AssertionError("forced-password-change gate did not block the user")
        user.must_change_password = False
        assert asyncio.run(get_current_user(user)) is user
    finally:
        db.close()
    print("password policy flow: PASS")


if __name__ == "__main__":
    main()
