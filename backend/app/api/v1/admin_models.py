"""Administrative model catalog; chat and model policy routes stay unchanged."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_admin
from app.database import get_db
from app.models.user import User
from app.services import model_governance


router = APIRouter(prefix="/admin/models", tags=["模型治理"])


class MetadataUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=0)
    display_name: str | None = Field(default=None, max_length=200)
    purpose: Literal["chat", "vision", "embedding", "other"] | None = None
    capabilities: list[str] | None = Field(default=None, max_length=20)
    context_length: int | None = Field(default=None, ge=1, le=10_000_000)
    enabled: bool = True
    input_price_per_million: Decimal | None = Field(default=None, ge=0, le=1000)
    output_price_per_million: Decimal | None = Field(default=None, ge=0, le=1000)
    cached_input_price_per_million: Decimal | None = Field(default=None, ge=0, le=1000)
    price_source: str | None = Field(default=None, max_length=300)


class ModelTestRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=1000)
    max_tokens: int = Field(default=64, ge=1, le=128)


@router.get("/catalog")
async def list_catalog(
    refresh: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=8, ge=1, le=100),
    _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    return model_governance.catalog(db, force=refresh, page=page, page_size=page_size)


@router.put("/catalog/metadata")
async def update_metadata(
    payload: MetadataUpdate, request: Request,
    _: User = Depends(require_admin), db: Session = Depends(get_db),
):
    row = model_governance.save_metadata(db, payload.model_dump(exclude_unset=True))
    request.state.audit_changes = {"model_metadata": "changed", "revision": row.revision}
    return {"ok": True, "revision": row.revision}


@router.post("/tests")
async def test_model(
    payload: ModelTestRequest, request: Request,
    current_user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    request.state.audit_changes = {"model_test": "bounded"}
    return await model_governance.bounded_test(
        db, current_user, payload.model_id, payload.prompt, payload.max_tokens,
    )
