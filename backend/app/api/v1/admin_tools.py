"""Separate reviewed MCP Tool directory; legacy MCP response shapes stay intact."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_admin, require_perm
from app.database import get_db
from app.models.tool_governance import AgentToolGrant
from app.models.user import User
from app.services import tool_governance


router = APIRouter(prefix="/admin", tags=["Tool 治理"])


class RiskUpdate(BaseModel):
    revision: int = Field(ge=0)
    risk: Literal["low", "high"]


class GrantChoice(BaseModel):
    server_id: int = Field(gt=0)
    tool_name: str = Field(min_length=1, max_length=200)
    schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentGrantUpdate(BaseModel):
    tools: list[GrantChoice] = Field(max_length=200)


@router.get("/mcp/servers/{server_id}/catalog")
def catalog(server_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return tool_governance.list_catalog(db, server_id)


@router.post("/mcp/servers/{server_id}/catalog/refresh")
async def refresh(server_id: int, request: Request, _: User = Depends(require_perm("mcp_manage.test")),
                  db: Session = Depends(get_db)):
    result = await tool_governance.refresh_catalog(db, server_id)
    request.state.audit_changes = {"permissions": "changed"} if result["changes"] else {}
    return result


@router.put("/mcp/servers/{server_id}/catalog/{tool_name}/risk")
def risk(server_id: int, tool_name: str, payload: RiskUpdate, request: Request,
         _: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = tool_governance.set_risk(db, server_id, tool_name, payload.revision, payload.risk)
    request.state.audit_changes = {"permissions": "changed"}
    return result


@router.get("/agents/{agent_id}/tools")
def grants(agent_id: int, _: User = Depends(require_perm("agent_manage.update")),
           db: Session = Depends(get_db)):
    return [{"server_id": row.server_id, "tool_name": row.tool_name,
             "schema_digest": row.schema_digest} for row in db.query(AgentToolGrant).filter_by(
                 agent_id=agent_id).order_by(AgentToolGrant.id)]


@router.put("/agents/{agent_id}/tools")
def save_grants(agent_id: int, payload: AgentGrantUpdate, request: Request,
                actor: User = Depends(require_perm("agent_manage.update")),
                db: Session = Depends(get_db)):
    result = tool_governance.replace_grants(db, agent_id, actor.id,
                                            [choice.model_dump() for choice in payload.tools])
    request.state.audit_changes = {"permissions": "changed"}
    return result
