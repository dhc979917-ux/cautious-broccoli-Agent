"""密码重置验证码的 Redis 存储（验证码天然短 TTL，适合放 Redis）。

键设计：
  pwdreset:{user_id}               码记录 {"h": sha256, "att": 尝试次数, "exp": 过期时间戳}，TTL 300s
  pwdreset_cd:{user_id}            发送冷却标记，TTL 60s
  pwdreset_day:{user_id}:{yyyymmdd} 当日发送计数，TTL 48h

Redis 不可用时，调用方（auth 路由）自动回退原 DB（password_reset_codes 表）路径。
"""
import time

from app.core.cache import cache_get, cache_set, cache_delete, cache_incr, get_client

CODE_TTL_SECONDS = 300   # 验证码 5 分钟有效
COOLDOWN_SECONDS = 60    # 同一用户 60 秒发送冷却
DAILY_LIMIT = 5          # 每日最多发送 5 次
MAX_ATTEMPTS = 5         # 单码最多校验 5 次


def available() -> bool:
    """Redis 是否可用（决定 auth 路由走 Redis 还是 DB 回退）。"""
    return get_client() is not None


def send_allowed(user_id: int) -> bool:
    """发送前置校验：冷却 + 每日上限。允许时顺手记录冷却标记并递增当日计数。
    与 DB 路径语义一致：超限时由调用方静默返回"已发送"。"""
    if cache_get(f"pwdreset_cd:{user_id}") is not None:
        return False
    day = time.strftime("%Y%m%d")
    count = cache_incr(f"pwdreset_day:{user_id}:{day}", ttl=172800)
    if count is not None and count > DAILY_LIMIT:
        return False
    cache_set(f"pwdreset_cd:{user_id}", 1, ttl=COOLDOWN_SECONDS)
    return True


def store_code(user_id: int, code_hash: str) -> None:
    """存入新码（覆盖旧码），TTL 即有效期。"""
    cache_set(
        f"pwdreset:{user_id}",
        {"h": code_hash, "att": 0, "exp": time.time() + CODE_TTL_SECONDS},
        ttl=CODE_TTL_SECONDS,
    )


def get_record(user_id: int):
    """取当前码记录；无码或已过期（TTL 到期自动消失）返回 None。"""
    return cache_get(f"pwdreset:{user_id}")


def bump_attempts(user_id: int) -> None:
    """校验失败 +1，按剩余有效期续写。"""
    rec = cache_get(f"pwdreset:{user_id}")
    if not rec:
        return
    ttl = int(rec.get("exp", 0) - time.time())
    if ttl <= 0:
        return
    rec["att"] = int(rec.get("att", 0)) + 1
    cache_set(f"pwdreset:{user_id}", rec, ttl=ttl)


def consume(user_id: int) -> None:
    """校验成功后销毁码（一次性）。"""
    cache_delete(f"pwdreset:{user_id}")
