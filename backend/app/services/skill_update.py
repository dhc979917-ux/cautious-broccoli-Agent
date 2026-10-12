import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.services import skill_versions
from app.services.skill_service import skill_mutation_lock
from app.services.skill_upload_service import extract_zip, plan_zip


def update_from_zip(db: Session, skill_id: int, zip_path: Path, filename: str, actor_id: int):
    from app.models.skill_resource import ResourceChangeJob
    if inspect(db.bind).has_table(ResourceChangeJob.__tablename__):
        from app.services import skill_resource_changes as changes
        job = changes.prepare_zip(db, 'skill', skill_id, actor_id, zip_path, filename)
        try:
            changes.confirm_job(db, job)
            changes.execute_job(db, job)
            return {'skill_id': skill_id, 'version_id': job.result['version_id'], 'message': job.message}
        except BaseException:
            db.rollback()
            db.refresh(job)
            if job.status != 'done':
                source, staging, backup = changes._job_paths(job)
                changes._cleanup_paths(source, staging, backup, restore=True)
                job.status = 'failed'
                job.message = '更新失败，原文件和版本保持不变'
                db.commit()
            raise
    with skill_mutation_lock():
        skill = db.get(Skill, skill_id, with_for_update=True)
        publication = db.get(SkillPublication, skill_id, with_for_update=True)
        if skill is None or not skill.is_active or publication is None:
            raise HTTPException(409, '技能不存在、已停用或尚未发布，不能更新')
        current = db.get(SkillVersion, publication.version_id)
        if current is None or current.dependencies:
            raise HTTPException(409, '此技能有声明的依赖，请通过版本配置完成依赖隔离测试后发布；原版本保持不变')
        revision = publication.revision
        plan = plan_zip(str(zip_path), filename)
        if 'SKILL.md' not in plan['rels'] or any(path.endswith('/SKILL.md') for path in plan['rels']):
            raise HTTPException(422, '更新包必须只包含一个技能，入口为根目录 SKILL.md；不能修改技能包成员结构')
        source = skill_versions.source_dir(skill)
        staging = source.parent / f'.staging-update-{uuid4().hex}'
        backup = source.parent / f'.backup-update-{uuid4().hex}'
        staging.mkdir()
        moved = False
        try:
            extract_zip(str(zip_path), plan['members'], plan['rels'], staging)
            version = skill_versions.create_snapshot(db, skill_id, actor_id, upload_source=staging)
            version.category = current.category
            version.tags = current.tags
            db.commit()
            result = skill_versions.sandbox_smoke_test(db, skill_id, version.id)
            if result['status'] != 'smoke_passed':
                raise HTTPException(409, '更新未通过隔离校验，原发布版本保持不变')
            source.rename(backup)
            staging.rename(source)
            moved = True
            skill_versions.publish(db, skill_id, version.id, revision, actor_id)
            return {'skill_id': skill_id, 'version_id': version.id, 'message': '更新已发布，新旧聊天下一轮均使用当前发布版'}
        except BaseException:
            db.rollback()
            if backup.exists():
                if moved and source.exists():
                    shutil.rmtree(source)
                if not source.exists():
                    backup.rename(source)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if backup.exists():
                shutil.rmtree(backup)
