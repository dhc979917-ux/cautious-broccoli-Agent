"""Whole-package history coordinated with existing per-Skill publications."""

import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, inspect

from app.config import settings
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillVersion
from app.models.skill_package import SkillPackage
from app.models.skill_resource import PackagePublication, PackageVersion
from app.services import skill_versions
from app.services.skill_resource_files import copy_tree, durable_directory, durable_tree, manifest, relative_path
from app.services.skill_service import SkillService


def storage_root():
    return Path(settings.KNOWLEDGE_DIR).resolve() / 'skill_package_versions'


def package_source(package):
    relative = relative_path(package.rel_path or '')
    if relative.parts[0] not in ('skills', 'skills_dialog', 'skills_task') or len(relative.parts) < 2:
        raise HTTPException(409, '技能包根目录无法确认')
    base = Path(SkillService._agents_dir()).resolve()
    candidate = base.joinpath(*relative.parts)
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.is_relative_to(base)):
        raise HTTPException(409, '技能包根目录包含符号链接')
    path = candidate.resolve()
    if not path.is_relative_to(base) or not path.is_dir():
        raise HTTPException(409, '技能包目录缺失，历史快照仍保留')
    return path


def member_map(db, package):
    prefix = package.rel_path.rstrip('/')
    result = {}
    for skill in db.query(Skill).filter_by(package_id=package.id).order_by(Skill.id):
        if not skill.is_active or skill.dir_missing:
            continue
        path = skill.rel_path or ''
        if path != prefix and not path.startswith(prefix + '/'):
            raise HTTPException(409, '包成员目录不属于当前技能包')
        publication = db.get(SkillPublication, skill.id)
        if publication is None:
            raise HTTPException(409, '包成员尚未建立发布基线')
        relative = path[len(prefix):].lstrip('/') or '.'
        result[relative] = {'skill_id': skill.id, 'version_id': publication.version_id,
                            'name': skill.name, 'active': True}
    return result


def create_version(db, package, source, members, actor_id):
    identifier = str(uuid4())
    number = (db.query(func.max(PackageVersion.number)).filter_by(package_id=package.id).scalar() or -1) + 1
    if db.query(PackageVersion.id).filter_by(package_id=package.id).first() is not None:
        number = db.query(func.max(PackageVersion.number)).filter_by(package_id=package.id).scalar() + 1
    destination = storage_root() / str(package.id) / identifier
    staging = storage_root() / str(package.id) / ('.staging-' + identifier)
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        entries = copy_tree(source, staging)
        durable_tree(staging)
        staging.rename(destination)
        durable_directory(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    version = PackageVersion(id=identifier, package_id=package.id, number=number,
                             snapshot_rel_path=destination.relative_to(storage_root()).as_posix(),
                             manifest=entries, content_hash=skill_versions._hash_manifest(entries),
                             members=members, created_by=actor_id)
    db.add(version)
    db.flush()
    return version


def verify_version(version):
    base = storage_root().resolve()
    path = base / version.snapshot_rel_path
    if path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_dir():
        raise HTTPException(503, '技能包快照缺失或路径不可信')
    if skill_versions._hash_manifest(version.manifest) != version.content_hash or manifest(path) != version.manifest:
        raise HTTPException(503, '技能包快照完整性校验失败')
    return path


def publish_package(db, package, version):
    publication = db.get(PackagePublication, package.id, with_for_update=True)
    if publication:
        publication.version_id = version.id
        publication.revision += 1
    else:
        publication = PackagePublication(package_id=package.id, version_id=version.id, revision=1)
        db.add(publication)
    db.flush()
    return publication


def record_member_publication(db, package_id, actor_id):
    if not inspect(db.bind).has_table(PackageVersion.__tablename__):
        return
    package = db.get(SkillPackage, package_id, with_for_update=True)
    if package is None:
        return
    db.flush()
    active = db.query(Skill).filter_by(package_id=package_id, is_active=True, dir_missing=False).all()
    if any(db.get(SkillPublication, skill.id) is None for skill in active):
        return
    members = member_map(db, package)
    current = db.get(PackagePublication, package.id)
    if current:
        previous = db.get(PackageVersion, current.version_id)
        if previous and previous.members == members:
            return previous
    source = package_source(package)
    overlay = source.parent / ('.staging-package-history-' + uuid4().hex)
    try:
        copy_tree(source, overlay)
        for relative, member in sorted(members.items(), key=lambda item: len(Path(item[0]).parts)):
            version = db.get(SkillVersion, member['version_id'])
            if version is None:
                raise HTTPException(409, '包成员发布版本无效')
            snapshot = skill_versions.verify_snapshot(version)
            target = overlay if relative == '.' else overlay / relative
            if target.exists() and relative != '.':
                shutil.rmtree(target)
            copy_tree(snapshot, target)
        version = create_version(db, package, overlay, members, actor_id)
        publish_package(db, package, version)
        return version
    finally:
        shutil.rmtree(overlay, ignore_errors=True)


def ensure_baseline(db, package_id, actor_id=None):
    package = db.get(SkillPackage, package_id, with_for_update=True)
    if package is None:
        raise HTTPException(404, '技能包不存在')
    if db.get(PackagePublication, package_id) is None:
        members = member_map(db, package)
        version = create_version(db, package, package_source(package), members, actor_id)
        publish_package(db, package, version)
        db.commit()
        return version
    return db.get(PackageVersion, db.get(PackagePublication, package_id).version_id)
