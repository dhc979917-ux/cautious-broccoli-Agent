"""One gateway for chat, embeddings, and auxiliary model calls."""

import logging
import threading
import time
from datetime import datetime, timezone

import requests

from app.config import settings

logger = logging.getLogger(__name__)
_catalog_lock = threading.Lock()
_catalog: tuple[float, list[str]] = (0.0, [])
_directory: tuple[float, list[dict], str | None] = (0.0, [], None)
_NON_CHAT = (
    "embedding", "ocr", "ovis", "asr", "rerank", "tts", "speech",
    "audio-generation", "image-generation", "image-gen",
)


def headers() -> dict[str, str]:
    if not settings.AI_GATEWAY_API_KEY:
        raise RuntimeError("AI_GATEWAY_API_KEY 未配置")
    return {"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"}


def endpoint(path: str) -> str:
    return f"{settings.AI_GATEWAY_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def gateway_directory(force: bool = False) -> dict:
    global _directory
    with _catalog_lock:
        expiry, cached, fetched_at = _directory
        if cached and not force and time.monotonic() < expiry:
            return {"models": [dict(entry) for entry in cached], "fetched_at": fetched_at,
                    "stale": False, "gateway_reachable": None}
        try:
            response = requests.get(endpoint("models"), headers=headers(), timeout=6)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid gateway catalog")
            entries = {}
            for item in payload["data"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                model_id = item["id"].strip()
                if not model_id or len(model_id) > 200:
                    continue
                provider = item.get("owned_by")
                entries[model_id] = {"model_id": model_id, "provider":
                                     provider[:120] if isinstance(provider, str) else None}
            if not entries:
                raise ValueError("Empty gateway catalog")
            models = [entries[key] for key in sorted(entries)]
            fetched_at = datetime.now(timezone.utc).isoformat()
            _directory = (time.monotonic() + 300, models, fetched_at)
            return {"models": [dict(entry) for entry in models], "fetched_at": fetched_at,
                    "stale": False, "gateway_reachable": True}
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            logger.warning("Model gateway directory unavailable (%s)", type(exc).__name__)
            if cached:
                return {"models": [dict(entry) for entry in cached], "fetched_at": fetched_at,
                        "stale": True, "gateway_reachable": False}
            raise RuntimeError("模型网关当前不可用") from exc


def chat_models(force: bool = False) -> list[str]:
    global _catalog
    with _catalog_lock:
        until, cached = _catalog
        if not force and cached and time.monotonic() < until:
            return list(cached)
        try:
            response = requests.get(endpoint("models"), headers=headers(), timeout=6)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("模型目录格式不正确")
            models = sorted({
                item["id"] for item in payload["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and not any(word in item["id"].lower() for word in _NON_CHAT)
            })
            if not models:
                raise ValueError("网关未返回聊天模型")
            _catalog = (time.monotonic() + 300, models)
            return list(models)
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.warning("模型目录获取失败，保留上一次有效结果: %s", exc)
            if cached:
                return list(cached)
            raise RuntimeError("模型网关当前不可用") from exc


def chat_completion(messages: list[dict], model: str, timeout: float = 90,
                    max_tokens: int | None = None) -> dict:
    payload = {"model": model, "messages": messages, "stream": False}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    response = requests.post(
        endpoint("chat/completions"), headers=headers(),
        json=payload, timeout=timeout,
    )
    response.raise_for_status()
    return response.json()
