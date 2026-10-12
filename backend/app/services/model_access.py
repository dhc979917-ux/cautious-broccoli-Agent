"""One allowlist decision for chat REST, WebSocket, and agent construction."""

import asyncio

from fastapi import HTTPException

from app.config import settings
from app.models.model_access_policy import ModelAccessPolicy
from app.services.model_permissions import role_models
from app.models.model_governance import ModelMetadata
from app.services.model_gateway import chat_models


async def available_catalog() -> list[str]:
    try:
        return await asyncio.to_thread(chat_models)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="模型网关暂不可用") from exc


def allowed_models(db, user, catalog=None) -> list[str]:
    catalog = catalog if catalog is not None else chat_models()
    disabled = {model_id for (model_id,) in db.query(ModelMetadata.model_id).filter(
        ModelMetadata.enabled.is_(False),
    ).all()}
    catalog = [model_id for model_id in catalog if model_id not in disabled]
    row = db.get(ModelAccessPolicy, ("user", str(user.id)))
    permitted = set(role_models(db, user.role, catalog))
    if row:
        permitted.update(row.allowed_models or [])
    return [name for name in catalog if name in permitted]


def effective_model(db, user, agent, override=None, catalog=None) -> str:
    choices = allowed_models(db, user, catalog)
    if override:
        if override not in choices:
            raise HTTPException(status_code=403, detail="当前账号无权使用该模型")
        return override
    for candidate in (agent.model_config_name, settings.MODEL_NAME):
        if candidate in choices:
            return candidate
    if choices:
        return choices[0]
    raise HTTPException(status_code=403, detail="当前账号未分配可用模型")
