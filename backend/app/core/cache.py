"""Redis 缓存接入层。

REDIS_URL 未配置或连接不可用时自动降级为 no-op：所有读写调用安全返回，
不向业务抛异常，本地开发零影响。业务侧统一使用本模块助手函数，不直接持有 redis 客户端。
"""
import json
import logging
import threading

from app.config import settings

logger = logging.getLogger("cache")

KEY_PREFIX = "taskpilot:"

_client = None
_initialized = False
_lock = threading.Lock()


def get_client():
    """返回已连接的 redis.Redis 实例；不可用时返回 None（单例，懒连接）。"""
    global _client, _initialized
    if _initialized:
        return _client
    with _lock:
        if _initialized:
            return _client
        _initialized = True
        url = (settings.REDIS_URL or "").strip()
        if not url:
            logger.info("[CACHE] REDIS_URL 未配置，未启用 Redis 缓存")
            return None
        try:
            import redis
            client = redis.Redis.from_url(
                url, decode_responses=True,
                socket_connect_timeout=3, socket_timeout=3,
            )
            client.ping()
            _client = client
            logger.info("[CACHE] Redis 已连接: %s", url.split("@")[-1])
        except Exception as e:
            logger.warning("[CACHE] Redis 不可用，缓存功能关闭: %s", e)
            _client = None
    return _client


def cache_get(key: str):
    """读取 JSON 缓存；未命中或任何异常都返回 None。"""
    client = get_client()
    if client is None:
        return None
    try:
        raw = client.get(KEY_PREFIX + key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None


def cache_set(key: str, value, ttl: int = 60) -> bool:
    """写入 JSON 缓存（带 TTL）；异常时返回 False，不影响业务。"""
    client = get_client()
    if client is None:
        return False
    try:
        client.set(KEY_PREFIX + key, json.dumps(value, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        return False


def cache_delete(key: str) -> None:
    """删除单个 key，静默失败。"""
    client = get_client()
    if client is None:
        return
    try:
        client.delete(KEY_PREFIX + key)
    except Exception:
        pass


def cache_delete_pattern(pattern: str) -> None:
    """按模式删 key（如 'skills:*'），用于写操作后的缓存失效。"""
    client = get_client()
    if client is None:
        return
    try:
        for k in client.scan_iter(match=KEY_PREFIX + pattern, count=100):
            client.delete(k)
    except Exception:
        pass


def cache_incr(key: str, ttl: int = None):
    """原子 +1（首次写入时设置 TTL）；返回计数，异常返回 None（业务应放行）。"""
    client = get_client()
    if client is None:
        return None
    try:
        full = KEY_PREFIX + key
        n = client.incr(full)
        if ttl and n == 1:
            client.expire(full, ttl)
        return int(n)
    except Exception:
        return None
