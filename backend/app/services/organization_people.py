"""Scope-filtered personnel queries with batched role, grant and usage loading."""

from collections import defaultdict

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.message import Message
from app.models.organization import Department, RoleTemplate, UserRoleAssignment
from app.models.session import Session as ChatSession
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.models.user_skill_access import UserSkillAccess
from app.services.organization_access import visible_users


SORT_COLUMNS = {
    "id": User.id,
    "username": User.username,
    "created_at": User.created_at,
    "last_login": User.last_login,
}


def _search(query, search: str):
    term = search.strip()
    if not term:
        return query
    escaped = term.replace("/", "//").replace("%", "/%").replace("_", "/_")
    pattern = f"%{escaped}%"
    return query.filter(or_(
        User.username.ilike(pattern, escape="/"),
        User.account.ilike(pattern, escape="/"),
        User.email.ilike(pattern, escape="/"),
    ))


def _bulk_person_details(db: Session, rows: list[User]) -> list[dict]:
    user_ids = [user.id for user in rows]
    if not user_ids:
        return []
    department_ids = {user.department_id for user in rows if user.department_id is not None}
    departments = dict(db.query(Department.id, Department.name).filter(
        Department.id.in_(department_ids),
    ).all()) if department_ids else {}
    agents = defaultdict(list)
    for user_id, agent_id in db.query(UserAgentAccess.user_id, UserAgentAccess.agent_id).filter(
        UserAgentAccess.user_id.in_(user_ids),
    ):
        agents[user_id].append(agent_id)
    skills = defaultdict(lambda: {"skill": [], "package": []})
    for user_id, target_type, target_id in db.query(
        UserSkillAccess.user_id, UserSkillAccess.target_type, UserSkillAccess.target_id,
    ).filter(UserSkillAccess.user_id.in_(user_ids)):
        if target_type in ("skill", "package"):
            skills[user_id][target_type].append(target_id)
    roles = defaultdict(list)
    for user_id, assignment_id, template_id, code, name, scope_type, scope_json in db.query(
        UserRoleAssignment.user_id, UserRoleAssignment.id,
        RoleTemplate.id, RoleTemplate.code, RoleTemplate.name,
        UserRoleAssignment.scope_type, UserRoleAssignment.scope_json,
    ).join(RoleTemplate, RoleTemplate.id == UserRoleAssignment.role_template_id).filter(
        UserRoleAssignment.user_id.in_(user_ids),
    ):
        roles[user_id].append({
            "id": assignment_id, "template_id": template_id,
            "code": code, "name": name,
            "scope_type": scope_type, "scope_json": scope_json,
        })
    sessions = dict(db.query(ChatSession.user_id, func.count(ChatSession.id)).filter(
        ChatSession.user_id.in_(user_ids),
    ).group_by(ChatSession.user_id).all())
    messages = dict(db.query(ChatSession.user_id, func.count(Message.id)).join(
        Message, Message.session_id == ChatSession.id,
    ).filter(ChatSession.user_id.in_(user_ids)).group_by(ChatSession.user_id).all())
    return [{
        "id": user.id,
        "username": user.username,
        "account": user.account,
        "email": user.email,
        "phone": user.phone,
        "role": user.role,
        "is_active": bool(user.is_active),
        "department_id": user.department_id,
        "department_name": departments.get(user.department_id),
        "job_title": user.job_title,
        "external_id": user.external_id,
        "source": user.source,
        "employment_status": user.employment_status,
        "revision": user.revision,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "agent_ids": sorted(agents[user.id]),
        "skill_grants": skills[user.id],
        "roles": roles[user.id],
        "usage": {"sessions": sessions.get(user.id, 0), "messages": messages.get(user.id, 0)},
    } for user in rows]


def get_person(db: Session, user: User) -> dict:
    return _bulk_person_details(db, [user])[0]


def list_people(db: Session, actor: User, *, page: int, page_size: int,
                search: str = "", department_id: int | None = None,
                role: str | None = None, template_id: int | None = None,
                status: str | None = None,
                sort_by: str = "last_login", sort_order: str = "desc") -> dict:
    query = _search(visible_users(db, actor), search)
    if department_id is not None:
        query = query.filter(User.department_id == department_id)
    if role:
        query = query.filter(User.role == role)
    if template_id is not None:
        assigned = db.query(UserRoleAssignment.user_id).filter(
            UserRoleAssignment.role_template_id == template_id,
        )
        query = query.filter(User.id.in_(assigned))
    if status == "active":
        query = query.filter(
            User.is_active.is_(True),
            or_(User.employment_status.is_(None), User.employment_status != "departed"),
        )
    elif status == "disabled":
        query = query.filter(User.is_active.is_(False))
    elif status == "departed":
        query = query.filter(User.employment_status == "departed")
    elif status == "unknown":
        query = query.filter(User.employment_status.is_(None))
    total = query.count()
    sort_column = SORT_COLUMNS[sort_by]
    ordering = sort_column.desc() if sort_order == "desc" else sort_column.asc()
    rows = query.order_by(ordering, User.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": _bulk_person_details(db, rows), "total": total, "page": page, "page_size": page_size}
