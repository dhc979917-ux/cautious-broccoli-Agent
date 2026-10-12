from fastapi import HTTPException

from app.models.model_access_policy import ModelAccessPolicy, ModelRoleOverride
from app.models.model_governance import GatewayModelEntry, ModelMetadata
from app.models.user import User


ROLES = ("viewer", "operator", "admin")


def default_allowed(role: str, model_id: str) -> bool:
    if role not in ROLES:
        return False
    if model_id.startswith("e"):
        return True
    if model_id.startswith("ali"):
        return role in ("operator", "admin")
    return role == "admin"


def role_models(db, role: str, catalog: list[str]) -> list[str]:
    overrides = {row.model_id: row.allowed for row in db.query(ModelRoleOverride).filter_by(role=role)}
    return [model_id for model_id in catalog
            if overrides.get(model_id, default_allowed(role, model_id))]


def permission_summary(db, catalog: list[str]) -> dict:
    configured = {role: set(role_models(db, role, catalog)) for role in ROLES}
    result = {}
    for model_id in catalog:
        permitted = [role for role in ROLES if model_id in configured[role]]
        minimum = next((role for index, role in enumerate(ROLES)
                        if permitted == list(ROLES[index:])), "custom")
        result[model_id] = {"minimum_role": minimum, "roles": permitted}
    return result


def extra_models(db, user: User, catalog: list[str]) -> list[str]:
    policy = db.get(ModelAccessPolicy, ("user", str(user.id)))
    baseline = set(role_models(db, user.role, catalog))
    requested = set(policy.allowed_models or []) if policy else set()
    return [model_id for model_id in catalog if model_id in requested and model_id not in baseline]


def display_names(db, catalog: list[str]) -> dict[str, str]:
    names = {row.model_id: (row.display_name or "").strip() for row in
             db.query(ModelMetadata).filter(ModelMetadata.model_id.in_(catalog))}
    return {model_id: names.get(model_id) or model_id for model_id in catalog}


def validate_principal(db, scope: str, principal: str):
    if scope == "role" and principal in ROLES:
        return None
    if scope == "user" and principal.isdecimal():
        user = db.get(User, int(principal), with_for_update=True)
        if user:
            return user
    raise HTTPException(status_code=400, detail="无效的授权角色或用户")


def set_role_override(db, role: str, model_id: str, allowed: bool | None):
    row = db.get(ModelRoleOverride, (role, model_id))
    if allowed is None or allowed == default_allowed(role, model_id):
        if row:
            db.delete(row)
    elif row:
        row.allowed = allowed
    else:
        db.add(ModelRoleOverride(role=role, model_id=model_id, allowed=allowed))


def set_extra_models(db, user: User, requested: list[str], catalog: list[str]):
    if set(requested) - set(catalog):
        raise HTTPException(status_code=400, detail="请选择网关中可用的聊天模型")
    baseline = set(role_models(db, user.role, catalog))
    extras = sorted(set(requested) - baseline)
    row = db.get(ModelAccessPolicy, ("user", str(user.id)))
    if not extras:
        if row:
            db.delete(row)
    elif row:
        row.allowed_models = extras
    else:
        db.add(ModelAccessPolicy(scope="user", principal=str(user.id), allowed_models=extras))


def change_model_permission(db, model_id: str, scope: str, principal: str,
                            allowed: bool | None, catalog: list[str]):
    entry = db.get(GatewayModelEntry, model_id, with_for_update=True)
    if entry is None:
        raise HTTPException(status_code=404, detail="网关目录没有该模型，请先刷新目录")
    if scope == "minimum_role":
        if principal not in ROLES or allowed is None:
            raise HTTPException(status_code=400, detail="请选择最低可用角色")
        minimum = ROLES.index(principal)
        for index, role in enumerate(ROLES):
            set_role_override(db, role, model_id, index >= minimum)
    elif scope == "role":
        validate_principal(db, scope, principal)
        set_role_override(db, principal, model_id, allowed)
    elif scope == "user":
        user = validate_principal(db, scope, principal)
        if model_id not in catalog:
            raise HTTPException(status_code=400, detail="仅可授权网关中的聊天模型")
        policy = db.get(ModelAccessPolicy, ("user", str(user.id)))
        requested = set(policy.allowed_models or []) if policy else set()
        if allowed:
            requested.add(model_id)
        else:
            requested.discard(model_id)
        set_extra_models(db, user, list(requested), catalog)
        user.revision = (user.revision or 0) + 1
    else:
        raise HTTPException(status_code=400, detail="不支持的授权范围")
    db.commit()


def authorization(db, model_id: str, catalog: list[str], user_id: int | None = None):
    configured = {role: model_id in role_models(db, role, [model_id]) for role in ROLES}
    overrides = {row.role for row in db.query(ModelRoleOverride).filter_by(model_id=model_id)}
    result = {"roles": [{"role": role, "allowed": configured[role],
                         "default_allowed": default_allowed(role, model_id),
                         "overridden": role in overrides} for role in ROLES],
              "is_chat": model_id in catalog, "user": None}
    if user_id is not None:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        policy = db.get(ModelAccessPolicy, ("user", str(user.id)))
        granted = model_id in (policy.allowed_models or []) if policy else False
        result["user"] = {"id": user.id, "role_allowed": configured.get(user.role, False),
                          "extra_allowed": granted, "allowed": configured.get(user.role, False) or granted}
    return result
