"""Administrative model visibility and bounded gateway tests."""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import requests
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.model_access_policy import ModelAccessPolicy
from app.models.model_governance import GatewayModelEntry, ModelMetadata, ModelTestRun
from app.models.user import User
from app.services.model_access import allowed_models
from app.services.model_permissions import permission_summary
from app.services.model_gateway import chat_completion, chat_models, gateway_directory


MAX_COST_USD = Decimal("0.05")
TESTS_PER_MINUTE = 3


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def catalog(db: Session, force: bool = False, page: int = 1, page_size: int = 8) -> dict:
    try:
        snapshot = gateway_directory(force=force)
    except RuntimeError:
        snapshot = {"models": [], "stale": True, "gateway_reachable": False,
                    "fetched_at": None}
    existing = {row.model_id: row for row in db.query(GatewayModelEntry).all()}
    present = {entry["model_id"] for entry in snapshot["models"]}
    if snapshot["gateway_reachable"] is True:
        seen_at = datetime.now(timezone.utc)
        for entry in snapshot["models"]:
            row = existing.get(entry["model_id"])
            if row is None:
                row = GatewayModelEntry(model_id=entry["model_id"])
                db.add(row)
                existing[entry["model_id"]] = row
            row.provider = entry["provider"]
            row.seen_at = seen_at
        db.commit()
    if not existing and not present:
        raise HTTPException(status_code=503, detail="模型网关不可用，且没有已保存的目录")
    metadata = {row.model_id: row for row in db.query(ModelMetadata).all()}
    latest = {}
    for run in db.query(ModelTestRun).order_by(ModelTestRun.tested_at.desc(), ModelTestRun.id.desc()).all():
        latest.setdefault(run.model_id, run)
    policies = db.query(ModelAccessPolicy).all()
    items = []
    permissions = permission_summary(db, sorted(existing.keys() | present))
    for model_id in sorted(existing.keys() | present):
        saved = metadata.get(model_id)
        test = latest.get(model_id)
        entry = existing.get(model_id)
        scopes = [{"scope": policy.scope, "principal": policy.principal}
                  for policy in policies if model_id in (policy.allowed_models or [])]
        items.append({
            "model_id": model_id,
            "provider": entry.provider if entry else None,
            "last_seen_at": _utc(entry.seen_at) if entry else None,
            "in_gateway": model_id in present if not snapshot["stale"] else None,
            "display_name": saved.display_name if saved else None,
            "purpose": (saved.purpose if saved else None) or "chat",
            "capabilities": saved.capabilities if saved else None,
            "context_length": saved.context_length if saved else None,
            "enabled": saved.enabled if saved else True,
            "input_price_per_million": str(saved.input_price_per_million) if saved and saved.input_price_per_million is not None else None,
            "output_price_per_million": str(saved.output_price_per_million) if saved and saved.output_price_per_million is not None else None,
            "cached_input_price_per_million": str(saved.cached_input_price_per_million) if saved and saved.cached_input_price_per_million is not None else None,
            "price_source": saved.price_source if saved else None,
            "price_updated_at": _utc(saved.price_updated_at) if saved else None,
            "revision": saved.revision if saved else 0,
            "authorized_scopes": scopes,
            **permissions[model_id],
            "last_test": {"tested_at": _utc(test.tested_at), "outcome": test.outcome,
                          "error_code": test.error_code, "duration_ms": test.duration_ms}
                         if test else None,
        })
    items.sort(key=lambda item: (not item["enabled"], item["model_id"]))
    fetched_at = snapshot["fetched_at"]
    if not fetched_at and existing:
        fetched_at = max(_utc(entry.seen_at) for entry in existing.values()).isoformat()
    return {"items": items[(page - 1) * page_size:page * page_size], "total": len(items),
            "page": page, "page_size": page_size, "fetched_at": fetched_at,
            "stale": snapshot["stale"], "gateway_reachable": snapshot["gateway_reachable"]}


def save_metadata(db: Session, payload: dict) -> ModelMetadata:
    model_id = payload.pop("model_id")
    revision = payload.pop("revision")
    if db.get(GatewayModelEntry, model_id) is None:
        raise HTTPException(status_code=404, detail="网关目录没有该模型，请先刷新目录")
    row = db.get(ModelMetadata, model_id, with_for_update=True)
    if row is None:
        row = ModelMetadata(model_id=model_id, revision=0)
        db.add(row)
    if row.revision != revision:
        raise HTTPException(status_code=409, detail="模型元数据已更新，请刷新后重试")
    for key, value in payload.items():
        setattr(row, key, value)
    if not row.purpose:
        row.purpose = "chat"
    if {"input_price_per_million", "output_price_per_million", "cached_input_price_per_million", "price_source"} & payload.keys():
        row.price_updated_at = datetime.now(timezone.utc)
    row.revision += 1
    db.commit()
    db.refresh(row)
    return row


def _redact_output(value) -> str:
    output = str(value)[:700]
    if settings.AI_GATEWAY_API_KEY:
        output = output.replace(settings.AI_GATEWAY_API_KEY, "[REDACTED]")
    output = re.sub(r"(?i)\b(?:sk|rk|api[_-]?key)[-_][a-z0-9_-]{8,}\b", "[REDACTED]", output)
    output = re.sub(r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b", "[EMAIL]", output)
    output = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", output)
    return output[:400]


async def bounded_test(db: Session, actor: User, model_id: str, prompt: str,
                       max_tokens: int, *, require_price: bool = True) -> dict:
    try:
        directory = await asyncio.to_thread(gateway_directory, True)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="模型网关暂不可用") from exc
    if directory["stale"]:
        raise HTTPException(status_code=503, detail="网关不可用，缓存目录不能用于在线测试")
    if model_id not in {entry["model_id"] for entry in directory["models"]}:
        raise HTTPException(status_code=404, detail="网关没有该模型")
    try:
        chat_catalog = await asyncio.to_thread(chat_models)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="聊天模型目录暂不可用") from exc
    if model_id not in chat_catalog:
        raise HTTPException(status_code=422, detail="该模型不在网关聊天模型列表中")
    if model_id not in allowed_models(db, actor, chat_catalog):
        raise HTTPException(status_code=403, detail="当前账号无权测试该模型")
    row = db.get(ModelMetadata, model_id)
    if row and not row.enabled:
        raise HTTPException(status_code=409, detail="该模型已由管理员停用")
    prices_known = bool(row and row.input_price_per_million is not None and
                        row.output_price_per_million is not None and row.price_source)
    if require_price and not prices_known:
        raise HTTPException(status_code=409, detail="价格或来源未知，无法保障测试费用上限")
    if prices_known:
        worst_input = len(prompt.encode("utf-8")) + 512
        worst_cost = ((Decimal(worst_input) * row.input_price_per_million +
                       Decimal(max_tokens) * row.output_price_per_million) / Decimal(1_000_000))
        if worst_cost > MAX_COST_USD:
            raise HTTPException(status_code=429, detail="预估测试费用超过单次上限")
    db.query(User).filter(User.id == actor.id).with_for_update().one()
    recent = db.query(ModelTestRun).filter(
        ModelTestRun.actor_id == actor.id,
        ModelTestRun.tested_at >= datetime.now(timezone.utc) - timedelta(minutes=1),
    ).count()
    if recent >= TESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="模型测试过于频繁，请稍后重试")
    trace_id = str(uuid4())
    run = ModelTestRun(model_id=model_id, actor_id=actor.id, trace_id=trace_id,
                       outcome="pending", tested_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(
            chat_completion, [{"role": "user", "content": prompt}], model_id,
            timeout=12, max_tokens=max_tokens,
        ), timeout=13)
        content = response["choices"][0]["message"]["content"]
        usage = response.get("usage") or {}
        run.input_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None
        run.output_tokens = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None
        run.outcome = "ok"
        output = _redact_output(content)
    except (requests.Timeout, asyncio.TimeoutError):
        run.outcome, run.error_code = "error", "gateway_timeout"
        output = None
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        run.outcome = "error"
        run.error_code = "quota_exceeded" if status_code == 429 else "gateway_unavailable" if status_code >= 500 else "gateway_error"
        output = None
    except (requests.RequestException, RuntimeError, KeyError, IndexError, TypeError, ValueError):
        run.outcome, run.error_code = "error", "gateway_error"
        output = None
    run.duration_ms = round((time.monotonic() - started) * 1000)
    db.commit()
    if run.outcome != "ok":
        status = {"gateway_timeout": 504, "quota_exceeded": 429,
                  "gateway_unavailable": 503}.get(run.error_code, 502)
        raise HTTPException(status_code=status, detail={"code": run.error_code,
                                                       "trace_id": trace_id})
    actual_cost = None
    if prices_known and run.input_tokens is not None and run.output_tokens is not None:
        actual_cost = str((Decimal(run.input_tokens) * row.input_price_per_million +
                           Decimal(run.output_tokens) * row.output_price_per_million) /
                          Decimal(1_000_000))
    return {"output": output, "duration_ms": run.duration_ms, "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens, "estimated_cost_usd": actual_cost,
            "trace_id": trace_id}
