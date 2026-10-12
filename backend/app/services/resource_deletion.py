from pathlib import Path
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.agent import Agent
from app.models.agent_governance import AgentVersion, SessionAgentVersion
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillReleaseEvent, SkillVersion
from app.services import skill_versions


def remove_resource_history(db, kind, identifier):
    from app.models.skill_resource import PackagePublication, PackageVersion, ResourceChangeJob
    if not inspect(db.connection()).has_table(ResourceChangeJob.__tablename__):
        return []
    jobs = db.query(ResourceChangeJob).filter_by(resource_type=kind, resource_id=identifier).all()
    if any(job.status not in ('done', 'failed', 'cancelled') for job in jobs):
        raise HTTPException(409, '资源仍有更新作业，请等待完成或取消更新后再彻底删除')
    for job in jobs:
        db.delete(job)
    paths = []
    if kind == 'package':
        from app.services.skill_package_versions import storage_root
        root = storage_root().resolve()
        path = root / str(identifier)
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path == root:
            raise HTTPException(409, '技能包历史路径不可信')
        if path.exists():
            paths.append(path)
        db.query(PackagePublication).filter_by(package_id=identifier).delete(synchronize_session=False)
        db.query(PackageVersion).filter_by(package_id=identifier).delete(synchronize_session=False)
    return paths


def remove_skill_history(db: Session, skill_ids: list[int], names: list[str]) -> list[Path]:
    if not inspect(db.connection()).has_table('skill_versions'):
        return []
    versions = db.query(SkillVersion).filter(SkillVersion.skill_id.in_(skill_ids)).all()
    version_ids = {version.id for version in versions}
    targets = db.query(Skill).filter(Skill.id.in_(skill_ids)).all()
    agents = {agent.id: agent for agent in db.query(Agent).all()} if inspect(db.connection()).has_table('agents') else {}
    blockers = {}

    def bound(config: dict, agent_type: str) -> bool:
        if set(names) & set(config.get('builtin_skills') or []):
            return True
        builtin_flag = 'builtin_task' if agent_type == 'task' else 'builtin_dialog'
        segment = 'skills_task' if agent_type == 'task' else 'skills_dialog'
        return any(getattr(skill, builtin_flag) or (skill.rel_path or '').split('/')[0] == segment for skill in targets)

    def add_reference(agent_id: int, source: str) -> None:
        agent = agents.get(agent_id)
        entry = blockers.setdefault(agent_id, {
            'id': agent_id, 'name': agent.name if agent else f'Agent #{agent_id}', 'references': [],
        })
        if source not in entry['references']:
            entry['references'].append(source)

    for agent in agents.values():
        if bound(agent.config or {}, agent.agent_type):
            add_reference(agent.id, '当前生效配置' if agent.is_active else '已删除助手的保留配置')
    if inspect(db.connection()).has_table('agent_versions'):
        pins = {}
        if inspect(db.connection()).has_table('session_agent_versions'):
            for pin in db.query(SessionAgentVersion).all():
                pins[pin.version_id] = pins.get(pin.version_id, 0) + 1
        for version in db.query(AgentVersion).all():
            if version.state != 'draft' and version.id not in pins:
                continue
            snapshot = version.snapshot or {}
            if bound(snapshot.get('config') or {}, snapshot.get('agent_type')):
                source = f'待应用配置 v{version.number}' if version.state == 'draft' else (
                    f'历史会话配置 v{version.number}（{pins[version.id]} 个会话）'
                )
                add_reference(version.agent_id, source)
    if blockers:
        agent_names = '、'.join(f'「{entry["name"]}」' for entry in blockers.values())
        raise HTTPException(409, {
            'code': 'skill_agent_bindings',
            'message': f'技能仍被助手 {agent_names} 内置绑定，不能彻底删除。请解除当前/待应用配置绑定并应用；若仅剩历史会话引用，请保留技能或删除相关会话。可选技能快照不算绑定。',
            'agents': list(blockers.values()),
        })
    for version in db.query(SkillVersion).filter(~SkillVersion.skill_id.in_(skill_ids)).all():
        if any(dependency.get('skill_id') in skill_ids or dependency.get('version_id') in version_ids for dependency in version.dependencies or []):
            raise HTTPException(409, '技能仍被其他技能依赖，请先解除依赖，再彻底删除。')
    if inspect(db.connection()).has_table('scheduled_skill_bindings'):
        from app.models.skill_hub import ScheduledSkillBinding
        if any(set(binding.versions.values()) & version_ids for binding in db.query(ScheduledSkillBinding)):
            raise HTTPException(409, '技能版本仍被定时任务固定绑定，请先解除绑定再彻底删除。')
    root = skill_versions.storage_root().resolve()
    paths = []
    for skill_id in skill_ids:
        paths.extend(remove_resource_history(db, 'skill', skill_id))
        path = root / str(skill_id)
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path == root:
            raise HTTPException(409, '技能快照路径异常，不能执行彻底删除。')
        if path.exists():
            paths.append(path)
    db.query(SkillReleaseEvent).filter(SkillReleaseEvent.skill_id.in_(skill_ids)).delete(synchronize_session=False)
    db.query(SkillPublication).filter(SkillPublication.skill_id.in_(skill_ids)).delete(synchronize_session=False)
    db.query(SkillVersion).filter(SkillVersion.skill_id.in_(skill_ids)).delete(synchronize_session=False)
    db.flush()
    return paths
