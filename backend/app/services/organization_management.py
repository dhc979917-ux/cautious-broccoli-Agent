"""Transactional organization and personnel changes, isolated from legacy user routes."""

import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.roles import all_permission_codes
from app.models.agent import Agent
from app.models.organization import Department, RoleTemplate, UserRoleAssignment
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.models.user_skill_access import UserSkillAccess
from app.services.organization_access import canonical_scope_json


class GovernanceError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def commit_change(db: Session):
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise GovernanceError(409, "组织标识或授权已存在") from error


def person_for_update(db: Session, person_id: int, revision: int) -> User:
    person = db.query(User).filter(User.id == person_id).with_for_update().first()
    if not person:
        raise GovernanceError(404, "人员不存在")
    if int(person.revision or 0) != revision:
        raise GovernanceError(409, "人员已被其他管理员修改，请刷新重试")
    return person


def lock_active_admins(db: Session) -> list[User]:
    return db.query(User).filter(User.role == "admin", User.is_active.is_(True)).order_by(
        User.id,
    ).with_for_update().all()


def ensure_last_admin(db: Session, person: User, *, removing: bool):
    if not removing or person.role != "admin" or not person.is_active:
        return
    admins = lock_active_admins(db)
    if len(admins) <= 1:
        raise GovernanceError(409, "不能禁用或降级最后一位管理员")


def _department(db: Session, department_id: int | None, *, forbid: int | None = None):
    if department_id is None:
        return None
    department = db.get(Department, department_id)
    if not department or not department.is_active:
        raise GovernanceError(400, "部门不存在或已停用")
    seen = {forbid} if forbid is not None else set()
    current = department
    while current is not None:
        if current.id in seen:
            raise GovernanceError(409, "部门层级不能形成环")
        seen.add(current.id)
        current = db.get(Department, current.parent_id) if current.parent_id else None
    return department


def create_department(db: Session, data) -> Department:
    _department(db, data.parent_id)
    department = Department(code=data.code, name=data.name.strip(), parent_id=data.parent_id)
    db.add(department)
    commit_change(db)
    db.refresh(department)
    return department


def update_department(db: Session, department_id: int, data) -> Department:
    department = db.query(Department).filter(Department.id == department_id).with_for_update().first()
    if not department:
        raise GovernanceError(404, "部门不存在")
    if department.revision != data.revision:
        raise GovernanceError(409, "部门已更新，请刷新重试")
    changed = data.model_fields_set - {"revision"}
    if ("name" in changed and data.name is None) or ("is_active" in changed and data.is_active is None):
        raise GovernanceError(400, "部门字段不能为空")
    if "parent_id" in changed:
        _department(db, data.parent_id, forbid=department_id)
        department.parent_id = data.parent_id
    if "name" in changed:
        if not data.name or not data.name.strip():
            raise GovernanceError(400, "部门名称不能为空")
        department.name = data.name.strip()
    if "is_active" in changed:
        if not data.is_active and (
            db.query(User.id).filter(User.department_id == department_id).first()
            or db.query(Department.id).filter(Department.parent_id == department_id).first()
        ):
            raise GovernanceError(409, "部门仍有成员或子部门")
        department.is_active = data.is_active
    if changed:
        department.revision += 1
        commit_change(db)
    return department


def _permission_json(permissions: list[str]) -> str:
    invalid = set(permissions) - all_permission_codes()
    if invalid:
        raise GovernanceError(400, "包含未知权限码")
    return json.dumps(list(dict.fromkeys(permissions)))


def create_template(db: Session, data) -> RoleTemplate:
    template = RoleTemplate(code=data.code, name=data.name.strip(),
                            permissions_json=_permission_json(data.permissions))
    db.add(template)
    commit_change(db)
    db.refresh(template)
    return template


def update_template(db: Session, template_id: int, data) -> tuple[RoleTemplate, list[int]]:
    template = db.query(RoleTemplate).filter(RoleTemplate.id == template_id).with_for_update().first()
    if not template:
        raise GovernanceError(404, "角色模板不存在")
    if template.revision != data.revision:
        raise GovernanceError(409, "角色模板已更新，请刷新重试")
    changed = data.model_fields_set - {"revision"}
    if any(getattr(data, field) is None for field in changed):
        raise GovernanceError(400, "角色字段不能为空")
    if "name" in changed:
        if not data.name or not data.name.strip():
            raise GovernanceError(400, "角色名称不能为空")
        template.name = data.name.strip()
    if "permissions" in changed:
        template.permissions_json = _permission_json(data.permissions)
    if "is_active" in changed:
        template.is_active = data.is_active
    affected = []
    if changed:
        template.revision += 1
        if changed & {"permissions", "is_active"}:
            affected = [user.id for user in db.query(User).join(
                UserRoleAssignment, UserRoleAssignment.user_id == User.id,
            ).filter(UserRoleAssignment.role_template_id == template_id).distinct().all()]
            db.query(User).filter(User.id.in_(affected)).update(
                {User.token_version: User.token_version + 1}, synchronize_session=False,
            )
        commit_change(db)
    return template, affected


def _validate_scope(db: Session, actor: User, scope_type: str, scope_json: str):
    if scope_type == "department" and actor.department_id is None:
        raise GovernanceError(400, "被授权人未分配部门")
    if scope_type == "departments":
        for department_id in json.loads(scope_json):
            _department(db, department_id)
    if scope_type == "resource":
        scope = json.loads(scope_json)
        models = {"user": User, "agent": Agent, "skill": Skill, "package": SkillPackage}
        model = models[scope["type"]]
        if db.query(model.id).filter(model.id.in_(scope["ids"])).count() != len(scope["ids"]):
            raise GovernanceError(400, "授权范围包含不存在的资源")


def grant_role(db: Session, person_id: int, data, granter: User) -> tuple[User, bool]:
    person = person_for_update(db, person_id, data.revision)
    if person.id == granter.id:
        raise GovernanceError(403, "不能修改自己的角色授权")
    template = db.get(RoleTemplate, data.template_id)
    if not template or not template.is_active:
        raise GovernanceError(400, "角色模板不存在或已停用")
    try:
        scope_json = canonical_scope_json(data.scope_type, data.scope)
    except ValueError as error:
        raise GovernanceError(400, str(error)) from error
    _validate_scope(db, person, data.scope_type, scope_json)
    existing = db.query(UserRoleAssignment).filter_by(
        user_id=person_id, role_template_id=data.template_id,
        scope_type=data.scope_type, scope_json=scope_json,
    ).first()
    if existing:
        raise GovernanceError(409, "角色授权已存在")
    db.add(UserRoleAssignment(user_id=person_id, role_template_id=data.template_id,
                              scope_type=data.scope_type, scope_json=scope_json,
                              granted_by=granter.id))
    person.revision += 1
    person.token_version += 1
    commit_change(db)
    return person, True


def revoke_role(db: Session, person_id: int, assignment_id: int, revision: int,
                granter: User) -> tuple[User, bool]:
    person = person_for_update(db, person_id, revision)
    if person.id == granter.id:
        raise GovernanceError(403, "不能修改自己的角色授权")
    assignment = db.query(UserRoleAssignment).filter_by(id=assignment_id, user_id=person_id).first()
    if not assignment:
        raise GovernanceError(404, "角色授权不存在")
    db.delete(assignment)
    person.revision += 1
    person.token_version += 1
    commit_change(db)
    return person, True


def update_person(db: Session, person_id: int, data, actor: User) -> tuple[User, bool]:
    if actor.role == "admin" and (
        data.role is not None or data.is_active is False or data.employment_status == "departed"
    ):
        lock_active_admins(db)
    person = person_for_update(db, person_id, data.revision)
    changed = data.model_fields_set - {"revision"}
    if any(getattr(data, field) is None for field in changed & {"role", "is_active"}):
        raise GovernanceError(400, "角色或启用状态不能为空")
    if actor.role != "admin" and changed - {"job_title"}:
        raise GovernanceError(403, "仅管理员可调整部门、身份与账号状态")
    if actor.id == person_id and (
        "role" in changed and data.role != "admin"
        or "is_active" in changed and data.is_active is False
        or "employment_status" in changed and data.employment_status == "departed"
    ):
        raise GovernanceError(403, "不能停用或降低自己的管理员角色")
    if "department_id" in changed:
        _department(db, data.department_id)
    proposed_role = data.role if "role" in changed else person.role
    proposed_active = data.is_active if "is_active" in changed else bool(person.is_active)
    if "employment_status" in changed and data.employment_status == "departed":
        if "is_active" in changed and data.is_active is True:
            raise GovernanceError(400, "离职人员不能保持启用")
        proposed_active = False
    if person.employment_status == "departed" and proposed_active and not (
        "employment_status" in changed and data.employment_status != "departed"
    ):
        raise GovernanceError(400, "离职人员启用前需先恢复在职状态")
    ensure_last_admin(db, person, removing=proposed_role != "admin" or not proposed_active)
    external_id = data.external_id if "external_id" in changed else person.external_id
    source = data.source if "source" in changed else person.source
    if bool(external_id) != bool(source):
        raise GovernanceError(400, "外部身份标识与来源必须同时填写或同时清空")
    if "source" in changed and source:
        source = source.strip()
    if "external_id" in changed and external_id:
        external_id = external_id.strip()
    if bool(external_id) != bool(source):
        raise GovernanceError(400, "外部身份标识与来源不能为空")
    before = (person.role, bool(person.is_active), person.department_id, person.employment_status)
    for field in changed - {"revision", "source", "external_id"}:
        if field not in {"role", "is_active", "department_id", "job_title", "employment_status"}:
            raise GovernanceError(400, "不支持的人员字段")
        setattr(person, field, getattr(data, field))
    if "source" in changed:
        person.source = source
    if "external_id" in changed:
        person.external_id = external_id
    person.is_active = proposed_active
    after = (person.role, bool(person.is_active), person.department_id, person.employment_status)
    revoke = changed and before != after
    if changed:
        person.revision += 1
        if revoke:
            person.token_version += 1
        commit_change(db)
    return person, bool(revoke)


def _validated_ids(db: Session, model, values: list[int], *, extra=(), existing=()) -> set[int]:
    if len(values) > 200 or any(type(value) is not int or value <= 0 for value in values):
        raise GovernanceError(400, "授权 ID 必须是正整数，且不能超过 200 个")
    unique = set(values)
    if not unique:
        return unique
    found = {row[0] for row in db.query(model.id).filter(
        model.id.in_(unique), model.is_active.is_(True), *extra,
    ).all()}
    if not unique <= (found | set(existing)):
        raise GovernanceError(400, "授权包含不存在或不可用的资源")
    return unique


def validate_agent_ids(db: Session, values: list[int], *, existing=()) -> set[int]:
    return _validated_ids(db, Agent, values, existing=existing)


def update_resources(db: Session, person_id: int, data, actor: User) -> tuple[User, bool]:
    person = person_for_update(db, person_id, data.revision)
    if actor.id == person.id:
        raise GovernanceError(403, "不能修改自己的资源授权")
    changed = data.model_fields_set - {"revision"}
    if any(getattr(data, field) is None for field in changed):
        raise GovernanceError(400, "授权列表不能为空；清空请传空列表")
    current_agents = {row.agent_id for row in db.query(UserAgentAccess).filter_by(user_id=person_id)}
    current_skills = {(row.target_type, row.target_id) for row in db.query(UserSkillAccess).filter_by(user_id=person_id)}
    agents = validate_agent_ids(db, data.agent_ids, existing=current_agents) if "agent_ids" in changed else None
    skills = _validated_ids(db, Skill, data.skill_ids, extra=(
        Skill.builtin_dialog.is_(False), Skill.builtin_task.is_(False),
    ), existing=(target_id for target_type, target_id in current_skills if target_type == "skill")) if "skill_ids" in changed else None
    packages = _validated_ids(db, SkillPackage, data.package_ids,
                              existing=(target_id for target_type, target_id in current_skills if target_type == "package")) if "package_ids" in changed else None
    from app.services.skill_hub.access import managed_map
    personal_ids = set(managed_map(db))
    personal_packages = {s.package_id for s in db.query(Skill).filter(Skill.id.in_(personal_ids)) if s.package_id}
    if set(skills or []) & personal_ids or set(packages or []) & personal_packages:
        raise GovernanceError(400, '个人技能只能通过作者分享，不能通过系统授权开放')
    if agents is not None:
        levels = {row.agent_id: row.access_level for row in db.query(UserAgentAccess).filter_by(user_id=person_id)}
        db.query(UserAgentAccess).filter_by(user_id=person_id).delete(synchronize_session=False)
        db.add_all(UserAgentAccess(user_id=person_id, agent_id=agent_id,
                                   access_level=levels.get(agent_id, "execute"))
                   for agent_id in sorted(agents))
    for target_type, ids in (("skill", skills), ("package", packages)):
        if ids is not None:
            db.query(UserSkillAccess).filter_by(user_id=person_id, target_type=target_type).delete(
                synchronize_session=False,
            )
            db.add_all(UserSkillAccess(user_id=person_id, target_type=target_type, target_id=value)
                       for value in sorted(ids))
    if changed:
        person.revision += 1
        person.token_version += 1
        commit_change(db)
    return person, bool(changed)
