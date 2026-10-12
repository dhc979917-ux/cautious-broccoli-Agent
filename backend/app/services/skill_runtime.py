import copy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.services.skill_service import SkillService


@dataclass(frozen=True)
class RuntimeSkillPlan:
    versions: dict[str, str]
    optional: tuple[tuple[Path, str], ...]
    builtin: tuple[Path, ...]
    personal_loaders: dict
    personal_seeds: tuple[Path, ...]


def validate_selection(db: Session, user, selected: list[str] | None) -> list[str]:
    available = {skill.name for skill in SkillService(db).get_usable_skills_for_user(user)}
    if selected is None:
        return sorted(available)
    rejected = set(selected) - available
    if rejected:
        raise HTTPException(409, '所选技能不可用、未发布或没有权限，请刷新技能列表：' + '、'.join(sorted(rejected)))
    return sorted(set(selected))


def resolve_plan(db: Session, agent_model, selected: list[str] | None,
                 user_role: str, builtin_root: str | None, user_id: int | None = None) -> RuntimeSkillPlan:
    from app.core.roles import ROLE_LEVELS
    from app.services.skill_versions import verify_snapshot, validate_dependencies

    from app.models.user import User
    from app.services.skill_hub import access as hub_access
    actor = db.get(User, user_id) if user_id else None
    mapping = hub_access.managed_map(db)
    authorized = {skill.id for skill in SkillService(db).get_usable_skills_for_user(actor)} if actor else set()
    fixed = (agent_model.config or {}).get('_scheduled_skill_versions', {})
    inactive_packages = SkillService(db)._inactive_package_ids()
    extras = set((agent_model.config or {}).get('builtin_skills') or [])
    requested = set(selected) if selected is not None else None
    builtin_flag = {'skills_task': 'builtin_task', 'skills_dialog': 'builtin_dialog'}.get(
        Path(builtin_root).name if builtin_root else '')
    optional = []
    builtin = []
    versions = {}
    found = set()
    personal_loaders = {}
    personal_seeds = set()
    for skill in db.query(Skill).filter(Skill.is_active.is_(True)).order_by(Skill.id):
        if skill.package_id in inactive_packages or skill.dir_missing:
            continue
        is_builtin = bool(builtin_flag and getattr(skill, builtin_flag)) or skill.name in extras
        is_optional = not skill.builtin_task and not skill.builtin_dialog and (
            requested is None or skill.name in requested)
        if not is_builtin and not is_optional:
            continue
        if requested is None and not is_builtin and ROLE_LEVELS.get(user_role, 0) < ROLE_LEVELS.get(skill.required_role, 0):
            continue
        managed = mapping.get(skill.id)
        if managed:
            if skill.id not in authorized or actor is None:
                if requested is not None and skill.name in requested:
                    raise HTTPException(403, '无权加载个人技能')
                continue
            selected_version_id = hub_access.runtime_version(db, managed, skill.id, actor)
            if fixed.get(str(skill.id)):
                selected_version_id = fixed[str(skill.id)]
                if not hub_access.permits_native(db, managed, skill.id, selected_version_id, actor):
                    raise HTTPException(409, '固定技能版本已安全停用')
            publication = SimpleNamespace(version_id=selected_version_id) if selected_version_id else None
        else:
            if actor and not is_builtin and skill.id not in authorized:
                if requested is not None and skill.name in requested:
                    raise HTTPException(403, '无权加载系统技能')
                continue
            publication = db.get(SkillPublication, skill.id)
            if publication and fixed.get(str(skill.id)):
                publication = SimpleNamespace(version_id=fixed[str(skill.id)])
        if publication is None:
            if is_builtin or requested is not None:
                raise HTTPException(409, f'技能尚未发布：{skill.display_name_zh or skill.display_name}')
            continue
        version = db.get(SkillVersion, publication.version_id)
        if version is None or version.skill_id != skill.id or version.status != 'published':
            raise HTTPException(409, f'技能发布版本无效：{skill.display_name_zh or skill.display_name}')
        directory = verify_snapshot(version)
        validate_dependencies(db, version)
        if actor:
            hub_access.check_dependencies(db, version, actor)
        versions[str(skill.id)] = version.id
        found.add(skill.name)
        if is_optional:
            parts = Path(skill.rel_path or '').parts
            top = ('personal_' + managed.id.replace('-', '_')) if managed else (parts[1] if len(parts) > 1 else skill.name)
            if managed:
                from app.services.skill_hub.content import version_directory
                if managed.kind == 'package':
                    pinned = managed.published_version_id if managed.owner_id == actor.id else next(s.version_id for s in hub_access.shares_for(db, actor) if s.resource_id == managed.id)
                    if fixed:
                        from app.models.skill_hub import SkillHubVersion
                        from app.models.skill_resource import PackageVersion
                        for hub_version in db.query(SkillHubVersion).filter_by(resource_id=managed.id, state='published'):
                            candidate = db.get(PackageVersion, hub_version.id)
                            if any(member['version_id'] == version.id for member in candidate.members.values()):
                                pinned = candidate.id
                                break
                    package_version = hub_access.native(db, managed, pinned)
                    package_directory = version_directory(db, managed, package_version)
                    relative = next(path for path, entry in package_version.members.items() if entry['skill_id'] == skill.id)
                    directory = package_directory if relative == '.' else package_directory / relative
                    sandbox_path = '/workspace/skills/' + package_directory.name + ('' if relative == '.' else '/' + relative)
                    personal_seeds.add(package_directory)
                else:
                    sandbox_path = '/workspace/skills/' + directory.name
                    personal_seeds.add(directory)
                personal_loaders[str(directory)] = {'name': skill.name, 'title': skill.display_name,
                    'sandbox_path': sandbox_path}
            optional.append((directory, top))
        elif is_builtin:
            builtin.append(directory)
    missing = (extras | (requested or set())) - found
    if missing:
        raise HTTPException(409, '技能加载失败，请检查启停、归属及发布状态：' + '、'.join(sorted(missing)))
    return RuntimeSkillPlan(versions, tuple(optional), tuple(builtin), personal_loaders, tuple(sorted(personal_seeds)))


def prepare_agent(db: Session, agent_model, selected: list[str] | None, user_role: str, user_id: int | None = None):
    root = 'skills_task' if agent_model.agent_type == 'task' else 'skills_dialog'
    plan = resolve_plan(db, agent_model, selected, user_role, root, user_id)
    config = copy.deepcopy(agent_model.config or {})
    config['_skill_actor_id'] = user_id
    config['_hub_runtime_loaders'] = plan.personal_loaders
    config['_hub_runtime_seed_paths'] = [str(path) for path in plan.personal_seeds]
    config['_runtime_skill_versions'] = dict(plan.versions)
    fields = ('id', 'name', 'description', 'agent_type', 'model_config_name', 'system_prompt', 'is_active')
    return SimpleNamespace(**{field: getattr(agent_model, field, None) for field in fields},
                           config=config, _skill_runtime_plan=plan)
