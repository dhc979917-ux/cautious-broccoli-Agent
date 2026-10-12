"""Scoped OCR proxy for untrusted Agent sandboxes; never expose the gateway key."""

import base64
import binascii
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, HTTPException, Request
from jose import JWTError, jwt

from app.config import settings
from app.core.cache import get_client
from app.database import SessionLocal
from app.models.session import Session as ChatSession
from app.models.user import User
from app.services.model_gateway import endpoint, headers

router = APIRouter(prefix="/ocr", tags=["沙箱图像识别"])
_MAX_BODY = 11 * 1024 * 1024
_MAX_IMAGE = 8 * 1024 * 1024
_IMAGE_TYPES = {"png", "jpeg", "webp", "gif", "bmp"}
_TEXT_PROMPT = "提取图片内的全部文字和表格，忠实保留结构；不要编造看不清的内容。"
_CHART_PROMPT = "说明图表类型、图例、坐标轴、可读数值与异常趋势；看不清的内容不要猜测。"


def mint_ocr_token(user_id: int, session_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    return jwt.encode(
        {"kind": "sandbox-ocr", "uid": user_id, "sid": session_id, "exp": expires},
        settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
    )


def _authorize(request: Request) -> tuple[int, int]:
    bearer = request.headers.get("Authorization", "")
    if not bearer.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 OCR 凭证")
    try:
        data = jwt.decode(
            bearer[7:], settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
        )
        if data.get("kind") != "sandbox-ocr":
            raise ValueError("invalid scope")
        user_id, session_id = int(data["uid"]), int(data["sid"])
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="OCR 凭证已失效") from exc
    with SessionLocal() as db:
        user = db.get(User, user_id)
        session = db.get(ChatSession, session_id)
        if user is None or not user.is_active or session is None or session.user_id != user_id:
            raise HTTPException(status_code=403, detail="会话无权使用 OCR")
    return user_id, session_id


def _limit(user_id: int, session_id: int) -> None:
    client = get_client()
    if client is None:
        raise HTTPException(status_code=503, detail="OCR 限额服务不可用")
    minute = int(time.time() // 60)
    try:
        with client.pipeline() as pipeline:
            pipeline.incr(f"taskpilot:ocr:{user_id}:{session_id}:{minute}")
            pipeline.expire(f"taskpilot:ocr:{user_id}:{session_id}:{minute}", 70)
            pipeline.incr(f"taskpilot:ocr:total:{user_id}:{session_id}")
            pipeline.expire(f"taskpilot:ocr:total:{user_id}:{session_id}", 7200)
            count, _, total, _ = pipeline.execute()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="OCR 限额服务不可用") from exc
    if count > 12 or total > 180:
        raise HTTPException(status_code=429, detail="OCR 调用次数超出安全限额")


def _ocr_model() -> str:
    response = requests.get(endpoint("models"), headers=headers(), timeout=6)
    response.raise_for_status()
    for item in response.json().get("data", []):
        model = item.get("id", "") if isinstance(item, dict) else ""
        if isinstance(model, str) and "ocr" in model.lower():
            return model
    raise RuntimeError("网关未配置 OCR 模型")


def _recognize(image_uri: str, mode: str) -> dict:
    prompt = _CHART_PROMPT if mode == "chart" else _TEXT_PROMPT
    response = requests.post(
        endpoint("chat/completions"), headers=headers(),
        json={"model": _ocr_model(), "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_uri}},
            {"type": "text", "text": prompt},
        ]}], "stream": False}, timeout=90,
    )
    response.raise_for_status()
    result = response.json()
    choice = (result.get("choices") or [{}])[0]
    return {"choices": [{"message": choice.get("message") or {}}],
            "model": result.get("model"), "usage": result.get("usage") or {}}


@router.post("/recognize")
async def recognize_image(request: Request):
    user_id, session_id = _authorize(request)
    if int(request.headers.get("content-length") or 0) > _MAX_BODY:
        raise HTTPException(status_code=413, detail="图片过大")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="图片过大")
    try:
        payload = json.loads(body)
        image_uri = payload["image"]
        mode = payload.get("mode", "text")
        prefix, encoded = image_uri.split(",", 1)
        image_type = prefix.removeprefix("data:image/").removesuffix(";base64")
        if image_type not in _IMAGE_TYPES or prefix != f"data:image/{image_type};base64":
            raise ValueError("unsupported image")
        raw_image = base64.b64decode(encoded, validate=True)
        if not raw_image or len(raw_image) > _MAX_IMAGE or mode not in ("text", "chart"):
            raise ValueError("invalid image or mode")
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="图片或识别模式无效") from exc
    _limit(user_id, session_id)
    try:
        import asyncio
        return await asyncio.to_thread(_recognize, image_uri, mode)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="云端 OCR 暂不可用，请尝试本地 OCR") from exc
