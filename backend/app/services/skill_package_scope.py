from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.services.skill_service import ROOT_BY_SCOPE, skill_mutation_lock


def change_package_scope(db: Session, package_id: int, scope: str):
    if scope not in ROOT_BY_SCOPE:
        raise HTTPException(422, '请选择可选、工具助手内置或数据分析师内置')
    with skill_mutation_lock():
        package = db.get(SkillPackage, package_id, with_for_update=True)
        if package is None or not package.is_active or package.dir_missing:
            raise HTTPException(409, '技能包已删除或目录缺失，不能修改内置归属')
        members = db.query(Skill).filter(Skill.package_id == package_id).all()
        available = [member for member in members if member.is_active and not member.dir_missing]
        if not available:
            raise HTTPException(409, '技能包没有有效成员或成员目录缺失')
        for member in members:
            member.builtin_dialog = scope == 'dialog'
            member.builtin_task = scope == 'task'
            member.scope_locked = True
        db.commit()
        return package
