"""Gateway catalog and per-role/per-user chat model permissions."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, require_admin
from app.database import get_db
from app.models.model_access_policy import ModelAccessPolicy
from app.models.user import User
from app.services.model_access import allowed_models
from app.services.model_gateway import chat_models
from app.services.model_permissions import (
    ROLES, authorization, change_model_permission, display_names, extra_models,
    role_models, set_extra_models, set_role_override, validate_principal,
)

router = APIRouter(prefix="/models", tags=["模型"])


class PolicyUpdate(BaseModel):
    scope: str
    principal: str
    allowed_models: list[str] | None


@router.get("/")
async def list_available_models(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    catalog = await asyncio.to_thread(chat_models)
    choices = allowed_models(db, current_user, catalog)
    return {"models": choices, "display_names": display_names(db, choices),
            "default": settings.MODEL_NAME if settings.MODEL_NAME in choices else choices[0] if choices else ""}


@router.get("/policies")
async def get_policies(
    _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    catalog = await asyncio.to_thread(chat_models)
    rows = db.query(ModelAccessPolicy).filter_by(scope="user").all()
    return {"models": catalog, "display_names": display_names(db, catalog), "policies": [
        {"scope": "role", "principal": role, "allowed_models": role_models(db, role, catalog)}
        for role in ROLES
    ] + [{"scope": row.scope, "principal": row.principal, "allowed_models": row.allowed_models}
         for row in rows]}


@router.put("/policies")
async def update_policy(
    update: PolicyUpdate,
    request: Request,
    _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    target_user = validate_principal(db, update.scope, update.principal)
    catalog = await asyncio.to_thread(chat_models)
    if update.allowed_models is not None:
        if set(update.allowed_models) - set(catalog):
            raise HTTPException(status_code=400, detail="请选择网关中可用的聊天模型")
    if update.scope == "role":
        for model_id in catalog:
            set_role_override(db, update.principal, model_id,
                              None if update.allowed_models is None else model_id in update.allowed_models)
    else:
        set_extra_models(db, target_user, update.allowed_models or [], catalog)
        target_user.revision = (target_user.revision or 0) + 1
    db.commit()
    request.state.audit_changes = {"allowed_models": "changed"}
    return {"ok": True}


class SinglePermissionUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    scope: str
    principal: str
    allowed: bool | None


@router.get("/authorization")
async def get_authorization(model_id: str, user_id: int | None = None,
                            _: User = Depends(require_admin), db: Session = Depends(get_db)):
    catalog = await asyncio.to_thread(chat_models)
    return authorization(db, model_id, catalog, user_id)


@router.put("/authorization")
async def update_authorization(payload: SinglePermissionUpdate, request: Request,
                               _: User = Depends(require_admin), db: Session = Depends(get_db)):
    catalog = await asyncio.to_thread(chat_models)
    change_model_permission(db, payload.model_id, payload.scope, payload.principal, payload.allowed, catalog)
    request.state.audit_changes = {"model_permission": "changed", "model_id": payload.model_id}
    return {"ok": True}


class ExtraGrantsUpdate(BaseModel):
    revision: int = Field(ge=0)
    models: list[str] = Field(max_length=1000)


@router.get("/users/{user_id}/extra-grants")
async def get_extra_grants(user_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = validate_principal(db, "user", str(user_id))
    catalog = await asyncio.to_thread(chat_models)
    from app.models.model_governance import ModelMetadata
    disabled = {row.model_id for row in db.query(ModelMetadata).filter_by(enabled=False)}
    return {"models": extra_models(db, user, catalog), "catalog": catalog,
            "role_models": role_models(db, user.role, catalog), "disabled": sorted(disabled),
            "display_names": display_names(db, catalog), "revision": user.revision}


@router.put("/users/{user_id}/extra-grants")
async def update_extra_grants(user_id: int, payload: ExtraGrantsUpdate, request: Request,
                             _: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = validate_principal(db, "user", str(user_id))
    if user.revision != payload.revision:
        raise HTTPException(status_code=409, detail="人员授权已更新，请刷新后重试")
    catalog = await asyncio.to_thread(chat_models)
    set_extra_models(db, user, payload.models, catalog)
    user.revision += 1
    db.commit()
    request.state.audit_changes = {"model_extra_grants": "changed"}
    return {"ok": True, "revision": user.revision}
