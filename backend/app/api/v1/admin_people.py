"""Organization administration with explicit object-level authorization."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.roles import parse_permissions
from app.database import get_db
from app.models.organization import Department, RoleTemplate, UserRoleAssignment
from app.models.user import User
from app.schemas.admin_people import (
    DepartmentCreate, DepartmentUpdate, PersonUpdate, ResourceGrantsUpdate,
    RoleGrant, RoleRevoke, RoleTemplateCreate, RoleTemplateUpdate,
)
from app.services.organization_access import ScopeTarget, can_list_users, has_permission, visible_users
from app.services.organization_management import (
    GovernanceError, create_department, create_template, grant_role, revoke_role,
    update_department, update_person, update_resources, update_template,
)
from app.services.organization_people import get_person, list_people
from app.services.session_guard import revoke_user_sessions


def _require_bearer(credentials: HTTPAuthorizationCredentials | None = Depends(
    HTTPBearer(auto_error=False),
)):
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")


router = APIRouter(prefix="/admin", tags=["组织与人员"], dependencies=[Depends(_require_bearer)])


@router.get("/access")
def access(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"can_view_people": can_list_users(actor, db), "can_manage": actor.role == "admin"}


def _admin(actor: User):
    if actor.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以维护组织与授权")


def _listed(actor: User, db: Session):
    if not can_list_users(actor, db):
        raise HTTPException(status_code=403, detail="无人员查看权限")


def _target(db: Session, actor: User, person_id: int, permission: str) -> User:
    person = db.get(User, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="人员不存在")
    if not has_permission(actor, permission, db, ScopeTarget(
        user_id=person_id, department_id=person.department_id,
        resource_type="user", resource_id=person_id,
    )):
        raise HTTPException(status_code=403, detail="无此人员的操作权限")
    if actor.role != "admin" and person.role == "admin" and permission != "org_manage.view":
        raise HTTPException(status_code=403, detail="不能编辑管理员")
    return person


def _apply(operation, *args):
    try:
        return operation(*args)
    except GovernanceError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


def _department_data(department: Department) -> dict:
    return {"id": department.id, "code": department.code, "name": department.name,
            "parent_id": department.parent_id, "is_active": department.is_active,
            "revision": department.revision}


def _template_data(template: RoleTemplate) -> dict:
    return {"id": template.id, "code": template.code, "name": template.name,
            "permissions": parse_permissions(template.permissions_json),
            "is_active": template.is_active, "revision": template.revision}


@router.get("/departments")
def departments(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _listed(actor, db)
    query = db.query(Department)
    if actor.role != "admin":
        allowed = visible_users(db, actor).with_entities(User.department_id).distinct()
        query = query.filter(Department.id.in_(allowed))
    return [_department_data(item) for item in query.order_by(Department.id).all()]


@router.post("/departments")
def add_department(data: DepartmentCreate, request: Request,
                   actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    item = _apply(create_department, db, data)
    request.state.audit_changes = {"department": "changed"}
    return _department_data(item)


@router.put("/departments/{department_id}")
def edit_department(department_id: int, data: DepartmentUpdate, request: Request,
                    actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    item = _apply(update_department, db, department_id, data)
    request.state.audit_changes = {"department": "changed"}
    return _department_data(item)


@router.get("/role-templates")
def templates(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _listed(actor, db)
    query = db.query(RoleTemplate)
    if actor.role != "admin":
        query = query.filter(RoleTemplate.id.in_(
            db.query(UserRoleAssignment.role_template_id).filter(UserRoleAssignment.user_id == actor.id),
        ))
    return [_template_data(item) for item in query.order_by(RoleTemplate.id).all()]


@router.post("/role-templates")
def add_template(data: RoleTemplateCreate, request: Request,
                 actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    item = _apply(create_template, db, data)
    request.state.audit_changes = {"permissions": "changed"}
    return _template_data(item)


@router.put("/role-templates/{template_id}")
async def edit_template(template_id: int, data: RoleTemplateUpdate, request: Request,
                        actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    item, affected = _apply(update_template, db, template_id, data)
    request.state.audit_changes = {"permissions": "changed"}
    for user_id in affected:
        if user_id != actor.id:
            await revoke_user_sessions(db, user_id, already_bumped=True)
    return _template_data(item)


@router.get("/people")
def people(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
           search: str = Query("", max_length=120), department_id: int | None = Query(None, gt=0),
           role: Literal["admin", "operator", "viewer"] | None = None,
           template_id: int | None = Query(None, gt=0),
           status: Literal["active", "disabled", "departed", "unknown"] | None = None,
           sort_by: Literal["id", "username", "created_at", "last_login"] = "last_login",
           sort_order: Literal["asc", "desc"] = "desc",
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _listed(actor, db)
    return list_people(db, actor, page=page, page_size=page_size, search=search,
                       department_id=department_id, role=role, template_id=template_id,
                       status=status, sort_by=sort_by, sort_order=sort_order)


@router.get("/people/{person_id}")
def person(person_id: int, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _listed(actor, db)
    target = _target(db, actor, person_id, "org_manage.view")
    result = get_person(db, target)
    result["can_edit"] = actor.role == "admin" or (
        target.role != "admin" and has_permission(actor, "org_manage.update", db, ScopeTarget(
            user_id=person_id, department_id=target.department_id,
            resource_type="user", resource_id=person_id,
        ))
    )
    return result


@router.put("/people/{person_id}")
async def edit_person(person_id: int, data: PersonUpdate, request: Request,
                      actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _listed(actor, db)
    previous = _target(db, actor, person_id, "org_manage.update")
    before = {"role": previous.role, "is_active": bool(previous.is_active)}
    changed, revoke = _apply(update_person, db, person_id, data, actor)
    request.state.audit_changes = {
        "role": {"before": before["role"], "after": changed.role},
        "is_active": {"before": before["is_active"], "after": bool(changed.is_active)},
    }
    if revoke and changed.id != actor.id:
        await revoke_user_sessions(db, person_id, already_bumped=True)
    return get_person(db, changed)


@router.put("/people/{person_id}/resources")
async def edit_resources(person_id: int, data: ResourceGrantsUpdate, request: Request,
                         actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    changed, revoke = _apply(update_resources, db, person_id, data, actor)
    request.state.audit_changes = {"agent_grants": "changed", "skill_grants": "changed"}
    if revoke:
        await revoke_user_sessions(db, person_id, already_bumped=True)
    return get_person(db, changed)


@router.post("/people/{person_id}/roles")
async def add_role(person_id: int, data: RoleGrant, request: Request,
                   actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    changed, _ = _apply(grant_role, db, person_id, data, actor)
    request.state.audit_changes = {"permissions": "changed"}
    await revoke_user_sessions(db, person_id, already_bumped=True)
    return get_person(db, changed)


@router.delete("/people/{person_id}/roles/{assignment_id}")
async def remove_role(person_id: int, assignment_id: int, data: RoleRevoke, request: Request,
                      actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _admin(actor)
    changed, _ = _apply(revoke_role, db, person_id, assignment_id, data.revision, actor)
    request.state.audit_changes = {"permissions": "changed"}
    await revoke_user_sessions(db, person_id, already_bumped=True)
    return get_person(db, changed)
