"""Single permission resolver for legacy grants and optional scoped role templates."""

import json
from dataclasses import dataclass

from sqlalchemy import false, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.roles import covers, parse_permissions
from app.models.organization import RoleTemplate, UserRoleAssignment
from app.models.user import User


@dataclass(frozen=True)
class ScopeTarget:
    user_id: int | None = None
    department_id: int | None = None
    resource_type: str | None = None
    resource_id: int | None = None


def _valid_ids(values) -> list[int]:
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("Scope requires a nonempty list of at most 100 IDs")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("Scope IDs must be positive integers")
    return sorted(set(values))


def canonical_scope_json(scope_type: str, scope=None) -> str:
    if scope_type in ("global", "department", "self"):
        if scope not in (None, []):
            raise ValueError("This scope does not accept IDs")
        return "[]"
    if scope_type == "departments":
        return json.dumps(_valid_ids(scope))
    if scope_type == "resource":
        if not isinstance(scope, dict) or set(scope) != {"type", "ids"}:
            raise ValueError("A resource scope needs a type and IDs")
        if scope["type"] not in ("user", "agent", "skill", "package"):
            raise ValueError("Unsupported resource scope type")
        return json.dumps({"type": scope["type"], "ids": _valid_ids(scope["ids"])}, sort_keys=True)
    raise ValueError("Unsupported role scope")


def _scope_data(assignment: UserRoleAssignment):
    try:
        return json.loads(assignment.scope_json)
    except (ValueError, TypeError):
        return None


def _matches(user: User, assignment: UserRoleAssignment, target: ScopeTarget | None) -> bool:
    scope_data = _scope_data(assignment)
    if assignment.scope_type == "global":
        return scope_data == []
    if target is None:
        return False
    if assignment.scope_type == "department":
        return scope_data == [] and user.department_id is not None and target.department_id == user.department_id
    if assignment.scope_type == "departments":
        return (
            isinstance(scope_data, list)
            and target.department_id is not None
            and any(type(value) is int and value == target.department_id for value in scope_data)
        )
    if assignment.scope_type == "self":
        return scope_data == [] and target.user_id == user.id
    if assignment.scope_type == "resource":
        return (
            isinstance(scope_data, dict)
            and scope_data.get("type") == target.resource_type
            and isinstance(scope_data.get("ids"), list)
            and target.resource_id is not None
            and any(type(value) is int and value == target.resource_id for value in scope_data["ids"])
        )
    return False


def _active_assignments(db: Session, user: User):
    return (
        db.query(UserRoleAssignment, RoleTemplate)
        .join(RoleTemplate, UserRoleAssignment.role_template_id == RoleTemplate.id)
        .filter(UserRoleAssignment.user_id == user.id, RoleTemplate.is_active.is_(True))
        .all()
    )


def has_permission(user: User, permission: str, db: Session | None = None,
                   target: ScopeTarget | None = None) -> bool:
    if user.role == "admin" or any(covers(code, permission) for code in user.permissions_list):
        return True
    if not settings.GOVERNANCE_RBAC_ENABLED or db is None:
        return False
    return any(
        any(covers(code, permission) for code in parse_permissions(template.permissions_json))
        and _matches(user, assignment, target)
        for assignment, template in _active_assignments(db, user)
    )


def has_module_permission(user: User, module: str, db: Session | None = None) -> bool:
    if user.role == "admin" or any(
        code == module or code.startswith(module + ".") for code in user.permissions_list
    ):
        return True
    if not settings.GOVERNANCE_RBAC_ENABLED or db is None:
        return False
    return any(
        _matches(user, assignment, None)
        and any(code == module or code.startswith(module + ".")
                for code in parse_permissions(template.permissions_json))
        for assignment, template in _active_assignments(db, user)
    )


def can_list_users(user: User, db: Session, permission: str = "org_manage.view") -> bool:
    if user.role == "admin" or any(covers(code, permission) for code in user.permissions_list):
        return True
    if not settings.GOVERNANCE_RBAC_ENABLED:
        return False
    for assignment, template in _active_assignments(db, user):
        if not any(covers(code, permission) for code in parse_permissions(template.permissions_json)):
            continue
        scope = _scope_data(assignment)
        if assignment.scope_type in ("global", "self") and scope == []:
            return True
        if assignment.scope_type == "department" and scope == [] and user.department_id is not None:
            return True
        if assignment.scope_type == "departments" and isinstance(scope, list) and any(
            type(value) is int and value > 0 for value in scope
        ):
            return True
        if assignment.scope_type == "resource" and isinstance(scope, dict) and scope.get("type") == "user" and isinstance(scope.get("ids"), list) and any(
            type(value) is int and value > 0 for value in scope["ids"]
        ):
            return True
    return False


def visible_users(db: Session, actor: User, permission: str = "org_manage.view"):
    query = db.query(User)
    if actor.role == "admin" or any(covers(code, permission) for code in actor.permissions_list):
        return query
    if not settings.GOVERNANCE_RBAC_ENABLED:
        return query.filter(false())
    user_ids = set()
    department_ids = set()
    for assignment, template in _active_assignments(db, actor):
        if not any(covers(code, permission) for code in parse_permissions(template.permissions_json)):
            continue
        scope = _scope_data(assignment)
        if assignment.scope_type == "global" and scope == []:
            return query
        if assignment.scope_type == "self" and scope == []:
            user_ids.add(actor.id)
        if assignment.scope_type == "department" and scope == [] and actor.department_id is not None:
            department_ids.add(actor.department_id)
        if assignment.scope_type == "departments" and isinstance(scope, list):
            department_ids.update(value for value in scope if type(value) is int and value > 0)
        if assignment.scope_type == "resource" and isinstance(scope, dict) and scope.get("type") == "user" and isinstance(scope.get("ids"), list):
            user_ids.update(value for value in scope.get("ids", []) if type(value) is int and value > 0)
    if not user_ids and not department_ids:
        return query.filter(false())
    return query.filter(or_(User.id.in_(user_ids), User.department_id.in_(department_ids)))


def navigation_permissions(db: Session, actor: User) -> list[str]:
    codes = set(actor.permissions_list)
    if not settings.GOVERNANCE_RBAC_ENABLED:
        return sorted(codes)
    scoped_modules = {'org_manage', 'memory_manage', 'ops_manage'}
    for assignment, template in _active_assignments(db, actor):
        for code in parse_permissions(template.permissions_json):
            if _matches(actor, assignment, None) or (
                code.split('.')[0] in scoped_modules and visible_users(db, actor, code).first() is not None
            ):
                codes.add(code)
    return sorted(codes)
