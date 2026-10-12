"""Recoverable staged edits and atomic package/member publication."""

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import inspect

from app.database import SessionLocal
from app.config import settings
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.models.skill_package import SkillPackage
from app.models.skill_resource import PackagePublication, PackageVersion, ResourceChangeJob
from app.services import skill_package_versions as packages, skill_versions
from app.services.skill_resource_files import TEXT_LIMIT, copy_tree, durable_directory, durable_tree, manifest, preview, relative_path, safe_file
from app.services.skill_service import SkillService, skill_mutation_lock
from app.services.skill_upload_service import extract_zip, plan_zip


logger = logging.getLogger(__name__)
TERMINAL = ('done', 'failed', 'cancelled')


def resource(db, kind, identifier):
    model = {'skill': Skill, 'package': SkillPackage}.get(kind)
    if model is None:
        raise HTTPException(422, '资源类型无效')
    entity = db.get(model, identifier, with_for_update=True)
    if entity is None:
        raise HTTPException(404, '技能资源不存在')
    return entity


def current(db, kind, identifier):
    entity = resource(db, kind, identifier)
    if kind == 'package':
        packages.ensure_baseline(db, identifier)
    publication = db.get(PackagePublication if kind == 'package' else SkillPublication, identifier)
    if publication is None:
        raise HTTPException(409, '技能资源尚未建立发布基线')
    version = db.get(PackageVersion if kind == 'package' else SkillVersion, publication.version_id)
    if version is None:
        raise HTTPException(409, '技能资源发布版本失效')
    return entity, publication, version


def version_directory(version, kind):
    return packages.verify_version(version) if kind == 'package' else skill_versions.verify_snapshot(version)


def source_directory(entity, kind):
    return packages.package_source(entity) if kind == 'package' else skill_versions.source_dir(entity)


def _job_paths(job):
    relative = relative_path(job.source_rel_path)
    if relative.parts[0] not in ('skills', 'skills_dialog', 'skills_task') or len(relative.parts) < 2:
        raise HTTPException(409, '更新恢复目录不可信')
    base = Path(SkillService._agents_dir()).resolve()
    source = base.joinpath(*relative.parts)
    for path in (source, *source.parents):
        if path.is_relative_to(base) and path.is_symlink():
            raise HTTPException(409, '更新恢复目录包含符号链接')
    expected_stage = '.staging-resource-' + job.id
    expected_backup = '.backup-resource-' + job.id
    if job.staging_name != expected_stage or job.backup_name != expected_backup:
        raise HTTPException(409, '更新恢复目录记录无效')
    staging = base / relative.parts[0] / expected_stage
    backup = base / relative.parts[0] / expected_backup
    if staging.is_symlink() or backup.is_symlink():
        raise HTTPException(409, '更新工作目录包含符号链接')
    return source, staging, backup


def _member_paths(entries):
    return sorted({str(Path(entry['path']).parent).replace('\\', '/') for entry in entries
                   if Path(entry['path']).name == 'SKILL.md'})


def _diff(old, new):
    before = {entry['path']: entry for entry in old}
    after = {entry['path']: entry for entry in new}
    return [{'path': path, 'change': '新增' if path not in before else '移除' if path not in after else '修改'}
            for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)]


def _create_job(db, kind, identifier, actor_id, operation, version_id=None, revision=None):
    entity, publication, version = current(db, kind, identifier)
    if not entity.is_active or entity.dir_missing:
        raise HTTPException(409, '技能资源已停用或目录缺失，不能更新')
    if version_id is not None and (publication.version_id != version_id or publication.revision != revision):
        raise HTTPException(409, '当前发布版本已变化，请刷新后重试')
    source = source_directory(entity, kind)
    source_manifest = manifest(source)
    if source_manifest != version.manifest:
        raise HTTPException(409, '服务器文件与当前发布版不同，请先检查已有文件变化')
    if kind == 'skill' and entity.package_id:
        packages.ensure_baseline(db, entity.package_id, actor_id)
    pending = db.query(ResourceChangeJob).filter_by(resource_type=kind, resource_id=identifier).filter(~ResourceChangeJob.status.in_(TERMINAL)).first()
    if pending is not None:
        raise HTTPException(409, '此资源已有未完成的更新，请查看作业进度或取消更新计划后重试')
    identifier_job = str(uuid4())
    job = ResourceChangeJob(id=identifier_job, resource_type=kind, resource_id=identifier,
                            actor_id=actor_id, operation=operation, source_rel_path=entity.rel_path,
                            staging_name='.staging-resource-' + identifier_job,
                            backup_name='.backup-resource-' + identifier_job,
                            base_version_id=publication.version_id, base_revision=publication.revision,
                            plan={'source_hash': skill_versions._hash_manifest(source_manifest)})
    if kind == 'skill' and entity.package_id:
        package_publication = db.get(PackagePublication, entity.package_id)
        job.plan = {**job.plan, 'package_id': entity.package_id,
                    'package_revision': package_publication.revision}
    db.add(job)
    return job, entity, version, source


def prepare_edit(db, kind, identifier, actor_id, path, content, version_id, revision, sha256):
    with skill_mutation_lock():
        job, entity, version, source = _create_job(db, kind, identifier, actor_id, 'edit', version_id, revision)
        original = preview(version_directory(version, kind), path)
        data = content.encode('utf-8')
        if not original['editable'] or len(data) > TEXT_LIMIT or '\x00' in content:
            raise HTTPException(422, '仅可编辑不超过2MiB的UTF-8文本')
        if original['sha256'] != sha256:
            raise HTTPException(409, '文件已变化，请重新打开后编辑')
        if data == safe_file(source, path).read_bytes():
            db.rollback()
            return None
        if kind == 'package' and Path(path).name == 'SKILL.md' and str(Path(path).parent) not in version.members:
            raise HTTPException(409, '未注册的技能入口请通过整包更新处理')
        _, staging, _ = _job_paths(job)
        try:
            copy_tree(source, staging)
            safe_file(staging, path).write_bytes(data)
            job.plan = {**job.plan, 'files': [{'path': path, 'change': '修改'}],
                        'target_hash': skill_versions._hash_manifest(manifest(staging)), 'members': {}}
            job.status = 'queued'
            job.message = '文件已暂存，正在等待校验'
            db.commit()
            return job
        except BaseException:
            db.rollback()
            shutil.rmtree(staging, ignore_errors=True)
            raise


def prepare_zip(db, kind, identifier, actor_id, archive, filename, existing_job=None):
    with skill_mutation_lock():
        if existing_job is None:
            job, entity, version, source = _create_job(db, kind, identifier, actor_id, 'upload')
        else:
            job = existing_job
            entity, publication, version = current(db, kind, identifier)
            if publication.version_id != job.base_version_id or publication.revision != job.base_revision:
                raise HTTPException(409, '上传期间资源已更新，请重新上传')
            source = source_directory(entity, kind)
        plan = plan_zip(str(archive), filename)
        paths = plan['rels']
        if kind == 'skill' and ('SKILL.md' not in paths or any(path.endswith('/SKILL.md') for path in paths)):
            raise HTTPException(422, '单Skill更新包只能包含根目录SKILL.md及该技能文件')
        _, staging, _ = _job_paths(job)
        staging.mkdir()
        try:
            extract_zip(str(archive), plan['members'], paths, staging)
            seed = source / '.seed_version'
            if seed.is_file():
                (staging / '.seed_version').write_bytes(seed.read_bytes())
            entries = manifest(staging)
            members = _member_paths(entries)
            if kind == 'package' and not members:
                raise HTTPException(422, '技能包必须包含至少一个SKILL.md入口')
            existing = set(version.members) if kind == 'package' else {'.'}
            incoming = set(members)
            changes = {'新增': sorted(incoming - existing), '移除': sorted(existing - incoming),
                       '保留': sorted(existing & incoming)} if kind == 'package' else {}
            job.plan = {**job.plan, 'files': _diff(version.manifest, entries), 'members': changes,
                        'target_hash': skill_versions._hash_manifest(entries)}
            job.status = 'planned'
            job.message = '请确认文件与成员差异；移除成员会停用而非彻底删除'
            db.commit()
            return job
        except BaseException:
            db.rollback()
            shutil.rmtree(staging, ignore_errors=True)
            raise


def confirm_job(db, job):
    if job.status == 'queued' or job.status in TERMINAL:
        return job
    if job.status != 'planned':
        raise HTTPException(409, '此更新计划正在处理')
    created = job.created_at.replace(tzinfo=timezone.utc) if job.created_at.tzinfo is None else job.created_at
    if datetime.now(timezone.utc) - created > timedelta(minutes=30):
        raise HTTPException(409, '更新计划已过期，请重新上传')
    entity, publication, version = current(db, job.resource_type, job.resource_id)
    if publication.version_id != job.base_version_id or publication.revision != job.base_revision:
        raise HTTPException(409, '资源版本已变化，请重新上传生成差异计划')
    job.status = 'queued'
    job.message = '已确认，等待隔离校验'
    db.commit()
    return job


def _validate_candidate(db, skill, source, actor_id):
    entry = source / 'SKILL.md'
    try:
        text = entry.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise HTTPException(422, 'SKILL.md必须是可读取的UTF-8文本') from exc
    if not text.strip():
        raise HTTPException(422, 'SKILL.md不能为空')
    for path in source.rglob('*.py'):
        try:
            compile(path.read_bytes(), path.relative_to(source).as_posix(), 'exec')
        except (SyntaxError, UnicodeError, ValueError) as exc:
            raise HTTPException(422, 'Python脚本语法校验失败：' + path.relative_to(source).as_posix()) from exc
    publication = db.get(SkillPublication, skill.id)
    previous = db.get(SkillVersion, publication.version_id) if publication else None
    version = skill_versions.create_snapshot(db, skill.id, actor_id, upload_source=source, commit=False)
    if previous:
        version.dependencies = previous.dependencies
        version.category = previous.category
        version.tags = previous.tags
        version.owner_id = previous.owner_id
    script = None
    if version.dependencies:
        script = next((path for path in ('scripts/test.py', 'tests/test.py') if (source / path).is_file()), None)
        if script is None:
            raise HTTPException(409, '此Skill有版本依赖，更新包须提供scripts/test.py或tests/test.py并调用全部模拟依赖')
    result = skill_versions.sandbox_smoke_test(db, skill.id, version.id, script, commit=False)
    if result['status'] not in ('passed', 'smoke_passed'):
        raise HTTPException(409, '更新未通过依赖或隔离校验，原版本保持不变')
    return version


def _publish_skill(db, skill, source, actor_id):
    entries = manifest(source)
    publication = db.get(SkillPublication, skill.id)
    previous = db.get(SkillVersion, publication.version_id) if publication else None
    if previous and previous.manifest == entries:
        return previous
    version = _validate_candidate(db, skill, source, actor_id)
    skill_versions.publish(db, skill.id, version.id, publication.revision if publication else 0,
                           actor_id, commit=False, notify_package=False)
    return version


def _publish_package(db, package, source, actor_id):
    prefix = package.rel_path.rstrip('/')
    old_members = db.query(Skill).filter_by(package_id=package.id).all()
    by_path = {skill.rel_path[len(prefix):].lstrip('/') or '.': skill for skill in old_members
               if skill.rel_path and (skill.rel_path == prefix or skill.rel_path.startswith(prefix + '/'))}
    flags = {(bool(skill.builtin_dialog), bool(skill.builtin_task), bool(skill.scope_locked)) for skill in old_members if skill.is_active}
    inherited = next(iter(flags)) if len(flags) == 1 else (False, False, False)
    discovered = _member_paths(manifest(source))
    mapping = {}
    for relative in discovered:
        skill = by_path.get(relative)
        directory = source if relative == '.' else source / relative
        if skill is None:
            basename = Path(relative).name if relative != '.' else package.name
            name = basename[:100]
            if db.query(Skill.id).filter_by(name=name).first():
                raise HTTPException(409, '新增成员目录名已被其他Skill使用：' + basename + '；请使用唯一目录名后重新上传')
            metadata = SkillService._read_skill_md(str(directory)) or {}
            role = package.required_role or str(metadata.get('required_role') or 'viewer')
            if role not in ('viewer', 'operator', 'admin'):
                raise HTTPException(422, '新增成员所需权限无效')
            skill = Skill(name=name, display_name=str(metadata.get('name') or basename)[:200],
                          description=str(metadata.get('description') or '')[:500], required_role=role,
                          package_id=package.id, rel_path=prefix if relative == '.' else prefix + '/' + relative,
                          is_active=True, dir_missing=False, is_hidden=package.is_hidden,
                          builtin_dialog=inherited[0], builtin_task=inherited[1], scope_locked=inherited[2], created_by=actor_id)
            db.add(skill)
            db.flush()
        if not skill.is_active and relative in by_path and not skill.dir_missing:
            continue
        skill.is_active = True
        skill.dir_missing = False
        version = _publish_skill(db, skill, directory, actor_id)
        mapping[relative] = {'skill_id': skill.id, 'version_id': version.id, 'name': skill.name, 'active': True}
    for relative, skill in by_path.items():
        if relative not in discovered:
            skill.is_active = False
            skill.dir_missing = True
    if not mapping:
        raise HTTPException(409, '更新后没有可用成员，原包保持不变')
    version = packages.create_version(db, package, source, mapping, actor_id)
    packages.publish_package(db, package, version)
    return version


def _cleanup_paths(source, staging, backup, restore=False):
    if any(path.is_symlink() for path in (source, staging, backup)):
        raise HTTPException(409, '更新清理目录包含符号链接')
    if restore and backup.exists():
        if source.exists():
            shutil.rmtree(source)
        backup.rename(source)
    if staging.exists():
        shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)


def execute_job(db, job):
    with skill_mutation_lock(timeout=30):
        db.refresh(job, with_for_update=True)
        if job.status != 'queued':
            return
        source, staging, backup = _job_paths(job)
        entity, publication, version = current(db, job.resource_type, job.resource_id)
        if not entity.is_active or entity.dir_missing:
            raise HTTPException(409, '资源已停用或目录缺失，更新未生效')
        if publication.version_id != job.base_version_id or publication.revision != job.base_revision:
            raise HTTPException(409, '版本已变化，未覆盖其他人的修改')
        if job.plan.get('package_id'):
            package_publication = db.get(PackagePublication, job.plan['package_id'], with_for_update=True)
            if package_publication.revision != job.plan['package_revision']:
                raise HTTPException(409, '所属技能包已更新，请重新打开文件')
        if skill_versions._hash_manifest(manifest(source)) != job.plan['source_hash']:
            raise HTTPException(409, '服务器文件已变化，更新已停止')
        entries = manifest(staging)
        if skill_versions._hash_manifest(entries) != job.plan['target_hash']:
            raise HTTPException(409, '暂存文件已变化，更新已停止')
        if entries == version.manifest:
            job.status = 'done'
            job.result = {'version_id': version.id, 'unchanged': True}
            job.message = '内容未变化，无需创建新版本'
            db.commit()
            _cleanup_paths(source, staging, backup)
            return
        job.status = 'validating'
        job.message = '正在校验文件与隔离环境；尚未修改生效内容'
        db.commit()
        durable_tree(staging)
        committed = False
        try:
            if job.resource_type == 'package':
                new_version = _publish_package(db, entity, staging, job.actor_id)
            else:
                new_version = _publish_skill(db, entity, staging, job.actor_id)
                if entity.package_id:
                    package = db.get(SkillPackage, entity.package_id, with_for_update=True)
                    package_source = packages.package_source(package)
                    overlay = package_source.parent / ('.staging-package-update-' + uuid4().hex)
                    try:
                        copy_tree(package_source, overlay)
                        relative = Path(entity.rel_path).relative_to(package.rel_path)
                        target = overlay / relative
                        if target != overlay:
                            shutil.rmtree(target)
                        copy_tree(staging, target)
                        _publish_package(db, package, overlay, job.actor_id)
                    finally:
                        shutil.rmtree(overlay, ignore_errors=True)
            source.rename(backup)
            durable_directory(source.parent)
            durable_directory(backup.parent)
            staging.rename(source)
            durable_directory(source.parent)
            durable_directory(staging.parent)
            job.status = 'done'
            job.message = '更新已生效，新旧聊天下一轮使用新版'
            job.result = {'version_id': new_version.id, 'unchanged': False}
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            committed = True
        except BaseException:
            db.rollback()
            db.refresh(job)
            if not committed and job.status != 'done':
                _cleanup_paths(source, staging, backup, restore=True)
            raise
        try:
            skill_versions.invalidate_runtime()
            _cleanup_paths(source, staging, backup)
        except Exception as exc:
            logger.warning('Resource update committed; cleanup will retry on restart job=%s type=%s', job.id, type(exc).__name__)


def run_job(identifier):
    with SessionLocal() as db:
        job = db.get(ResourceChangeJob, identifier)
        if job is None or job.status != 'queued':
            return
        try:
            execute_job(db, job)
        except Exception as exc:
            db.rollback()
            job = db.get(ResourceChangeJob, identifier)
            if job.status == 'done':
                skill_versions.invalidate_runtime()
                return
            source, staging, backup = _job_paths(job)
            _cleanup_paths(source, staging, backup, restore=True)
            job.status = 'failed'
            job.message = (exc.detail if isinstance(exc, HTTPException) else '更新失败，原文件和版本保持不变')[:500]
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            logger.warning('Skill resource change failed job=%s type=%s', identifier, type(exc).__name__)


def run_upload_plan(identifier):
    with SessionLocal() as db:
        job = db.get(ResourceChangeJob, identifier)
        if job is None or job.status != 'planning':
            return
        name = job.plan['archive_name']
        if Path(name).name != name or not name.endswith('.zip'):
            raise RuntimeError('Untrusted upload archive')
        archive = Path(settings.KNOWLEDGE_DIR).resolve() / 'skill_resource_uploads' / name
        try:
            prepare_zip(db, job.resource_type, job.resource_id, job.actor_id, archive, job.plan['filename'], existing_job=job)
        except Exception as exc:
            db.rollback()
            job = db.get(ResourceChangeJob, identifier)
            source, staging, backup = _job_paths(job)
            _cleanup_paths(source, staging, backup, restore=True)
            job.status = 'failed'
            job.message = (exc.detail if isinstance(exc, HTTPException) else '更新包无法安全解析，原内容保持不变')[:500]
            db.commit()
        finally:
            archive.unlink(missing_ok=True)


def recover_changes():
    with SessionLocal() as db:
        if not inspect(db.bind).has_table(ResourceChangeJob.__tablename__):
            return
        for job in db.query(ResourceChangeJob):
            try:
                source, staging, backup = _job_paths(job)
            except HTTPException:
                logger.error('Unsafe resource recovery path skipped job=%s', job.id)
                if job.status not in TERMINAL:
                    job.status = 'failed'
                    job.message = '更新目录不可信，已停止自动恢复；请管理员检查目录'
                continue
            if job.status == 'done':
                _cleanup_paths(source, staging, backup)
            elif job.status not in ('failed', 'cancelled'):
                _cleanup_paths(source, staging, backup, restore=True)
                job.status = 'failed'
                job.message = '服务重启中断了更新，已恢复原文件和原发布版本；请重新提交'
                job.finished_at = datetime.now(timezone.utc)
            else:
                _cleanup_paths(source, staging, backup, restore=True)
            if job.plan.get('archive_name'):
                name = job.plan['archive_name']
                if Path(name).name != name:
                    raise RuntimeError('Untrusted recovery archive')
                (Path(settings.KNOWLEDGE_DIR).resolve() / 'skill_resource_uploads' / name).unlink(missing_ok=True)
        db.commit()
