"""Immutable personal content; reuse native Skill/package snapshots and publication pointers."""
import copy
import json
import re
import shutil
import tempfile
import zipfile
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import func
from app.config import settings
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_governance import SkillVersion, SkillPublication
from app.models.skill_resource import PackageVersion
from app.models.skill_hub import SkillHubResource, SkillHubVersion, SkillHubFork
from app.services import skill_versions, skill_package_versions as packages
from app.services.skill_resource_files import manifest, copy_tree, safe_file, relative_path, preview
from app.services.skill_hub import access

_TEST_SLOTS = threading.BoundedSemaphore(max(1, settings.SKILL_HUB_TEST_CONCURRENCY))

SENSITIVE = {'.env', 'id_rsa', 'id_ed25519', 'credentials.json', 'auth.json'}
SECRET = re.compile(r'(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*(?:[\"\']([A-Za-z0-9_\-/+]{20,})[\"\']|([A-Za-z0-9_\-/+]{20,})(?:\s|$))', re.I)


def has_secret(text):
    for candidate in SECRET.finditer(text):
        value = candidate.group(1) or candidate.group(2)
        if value.startswith(('your_', 'example_', 'placeholder_', 'test_', 'sample_', '_required_', 'os.environ')):
            continue
        return True
    return False



@contextmanager
def staging():
    root = Path(settings.KNOWLEDGE_DIR).resolve() / 'skill_hub' / '.staging'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix='change-') as folder:
        yield Path(folder)


def version_directory(db, row, version):
    return packages.verify_version(version) if row.kind == 'package' else skill_versions.verify_snapshot(version)


def scan(root):
    entries = manifest(root)
    skill_files = [e['path'] for e in entries if Path(e['path']).name == 'SKILL.md']
    if not skill_files:
        raise HTTPException(422, '必须包含 SKILL.md')
    for entry in entries:
        path = root / entry['path']
        if path.name.lower() in SENSITIVE or path.suffix.lower() in {'.pem', '.key', '.p12'}:
            raise HTTPException(422, '技能包含疑似凭证文件，请先移除')
        if path.suffix.lower() in {'.md', '.py', '.json', '.yml', '.yaml', '.txt', '.sh', '.js', '.ts'}:
            if has_secret(path.read_text(encoding='utf-8', errors='replace')):
                raise HTTPException(422, '技能包含疑似明文凭证，请使用平台受控配置')
        if path.name == 'SKILL.md' and not path.read_text(encoding='utf-8').strip():
            raise HTTPException(422, 'SKILL.md 不能为空')
    return sorted(str(Path(p).parent).replace('\\', '/') for p in skill_files)


def _extract(archive, destination):
    total = 0
    if not zipfile.is_zipfile(archive):
        raise HTTPException(422, '文件不是有效的 ZIP 技能包')
    with zipfile.ZipFile(archive) as source:
        files = [x for x in source.infolist() if not x.is_dir()]
        if len(files) > 2000:
            raise HTTPException(413, '技能文件超过2000个限制')
        seen = set()
        for item in files:
            rel = relative_path(item.filename)
            if rel.as_posix() in seen or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise HTTPException(422, 'ZIP包含重复路径或符号链接')
            seen.add(rel.as_posix())
            total += item.file_size
            if total > 100 * 1024 * 1024 or (item.file_size > 1024 * 1024 and item.file_size > 200 * max(item.compress_size, 1)):
                raise HTTPException(413, '技能包过大或压缩比例异常')
            target = destination.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as src, target.open('wb') as out:
                shutil.copyfileobj(src, out)
    paths = scan(destination)
    children = list(destination.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0], paths
    return destination, paths


def extract(archive, destination):
    try:
        return _extract(archive, destination)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, NotImplementedError) as error:
        raise HTTPException(422, 'ZIP损坏、加密或使用了不支持的编码/压缩方式') from error


def assert_revision(row, revision):
    if row.revision != revision:
        raise HTTPException(409, '版本已变化，请重新打开后比较，不会覆盖其他修改')


def metadata_default(title, summary):
    return {'title': title, 'summary': summary, 'category': '其他', 'tags': [],
            'description': '', 'example': '', 'requirements': '', 'risks': '', 'license': '', 'dependencies': {}}


def freeze(db, row, source, actor_id, metadata=None):
    from app.models.user import User
    db.query(User).filter_by(id=row.owner_id).with_for_update().one()
    locations = scan(source)
    if row.kind == 'skill' and locations != ['.']:
        raise HTTPException(422, '独立技能必须在根目录包含唯一 SKILL.md；多技能请按整包导入')
    metadata = metadata or metadata_default(row.title, row.summary)
    used = sum(sum(e["bytes"] for e in access.native(db, r, v.id).manifest) for r in db.query(SkillHubResource).filter_by(owner_id=row.owner_id) for v in db.query(SkillHubVersion).filter_by(resource_id=r.id))
    if used + sum(e["bytes"] for e in manifest(source)) > settings.SKILL_HUB_MAX_STORAGE_MB * 1024 * 1024:
        raise HTTPException(413, "个人技能存储配额已用尽")
    old_members = {s.rel_path.split('/'+row.id+'/', 1)[-1]: s for s in db.query(Skill).filter_by(package_id=row.package_id)} if row.kind == 'package' else {}
    members = {}
    for location in locations:
        member_source = source if location == '.' else source / location
        if row.kind == 'skill':
            skill = db.get(Skill, row.skill_id)
        else:
            skill = old_members.get(location)
            if skill is None:
                skill = Skill(name='personal_'+uuid4().hex, display_name=Path(location).name if location != '.' else row.title,
                    description=row.summary, required_role='viewer', is_active=True, is_hidden=True,
                    builtin_dialog=False, builtin_task=False, scope_locked=True,
                    package_id=row.package_id, created_by=row.owner_id,
                    rel_path='skills/.personal/'+row.id+'/'+location)
                db.add(skill); db.flush()
        snapshot = skill_versions.create_snapshot(db, skill.id, actor_id, upload_source=member_source, commit=False)
        snapshot.dependencies = copy.deepcopy(metadata.get('dependencies', {}).get(location, []))
        snapshot.category = metadata.get('category')
        snapshot.tags = metadata.get('tags', [])
        members[location] = {'skill_id': skill.id, 'version_id': snapshot.id, 'name': skill.name}
    if row.kind == 'package':
        local = {entry['skill_id']: entry for entry in members.values()}
        fork_ids = metadata.get('_fork_member_ids', {})
        remap = {int(source_id): members[location]['skill_id'] for location, source_id in fork_ids.items() if location in members}
        declarations = {}
        for location, entry in members.items():
            snapshot_row = db.get(SkillVersion, entry['version_id'])
            dependencies = copy.deepcopy(snapshot_row.dependencies or [])
            for dependency in dependencies:
                if dependency.get('kind') == 'skill':
                    dependency['skill_id'] = remap.get(dependency.get('skill_id'), dependency.get('skill_id'))
                    if dependency['skill_id'] in local:
                        dependency['version_id'] = local[dependency['skill_id']]['version_id']
            snapshot_row.dependencies = dependencies
            declarations[location] = dependencies
        metadata = {**metadata, 'dependencies': declarations}
        metadata.pop('_fork_member_ids', None)
    native = packages.create_version(db, db.get(SkillPackage, row.package_id), source, members, actor_id) if row.kind == 'package' else snapshot
    number = (db.query(func.max(SkillHubVersion.number)).filter_by(resource_id=row.id).scalar() or 0) + 1
    version = SkillHubVersion(id=native.id, resource_id=row.id, number=number,
        skill_version_id=native.id if row.kind == 'skill' else None,
        package_version_id=native.id if row.kind == 'package' else None,
        metadata_json=copy.deepcopy(metadata), created_by=actor_id)
    db.add(version)
    row.draft_version_id = native.id
    row.revision += 1
    db.flush()
    return version


def make_resource(db, actor, title, summary, kind, source, origin='personal', source_row=None, source_version=None, metadata=None):
    from app.models.user import User
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    if db.query(SkillHubResource).filter_by(owner_id=actor.id).count() >= settings.SKILL_HUB_MAX_RESOURCES:
        raise HTTPException(409, "个人技能数量达到配额，请联系管理员")
    identifier = str(uuid4())
    row = SkillHubResource(id=identifier, kind=kind, owner_id=actor.id, title=title, summary=summary,
        origin=origin, allow_fork=True, source_resource_id=source_row.id if source_row else None,
        source_version_id=source_version.id if source_version else None,
        source_title=source_row.title if source_row else None)
    if kind == 'skill':
        entity = Skill(name='personal_'+uuid4().hex, display_name=title, description=summary,
            required_role='viewer', is_active=True, is_hidden=True, builtin_dialog=False, builtin_task=False,
            scope_locked=True, created_by=actor.id, rel_path='skills/.personal/'+identifier)
        db.add(entity); db.flush(); row.skill_id = entity.id
    else:
        entity = SkillPackage(name='personal_package_'+uuid4().hex, display_name=title, description=summary,
            is_active=True, is_hidden=True, created_by=actor.id, rel_path='skills/.personal/'+identifier)
        db.add(entity); db.flush(); row.package_id = entity.id
    db.add(row); db.flush()
    if source_row:
        db.add(SkillHubFork(source_resource_id=source_row.id, source_version_id=source_version.id,
            forked_resource_id=row.id, forked_by=actor.id, source_title=source_row.title))
    version = freeze(db, row, source, actor.id, metadata)
    if source_row:
        # Explicit package-internal Skill dependencies must point at the new members.
        original = source_version.members if source_row.kind == 'package' else {'.': {'skill_id': source_row.skill_id, 'version_id': source_version.id}}
        current = access.native(db, row, version.id)
        new_members = current.members if kind == 'package' else {'.': {'skill_id': row.skill_id, 'version_id': current.id}}
        mapping = {original[path]['skill_id']: target for path, target in new_members.items() if path in original}
        dependencies = {}
        for path, target in new_members.items():
            old = db.get(SkillVersion, original[path]['version_id']) if path in original else None
            declarations = copy.deepcopy(old.dependencies or []) if old else []
            for dependency in declarations:
                if dependency.get('kind') == 'skill' and dependency.get('skill_id') in mapping:
                    mapped = mapping[dependency['skill_id']]
                    dependency.update(skill_id=mapped['skill_id'], version_id=mapped['version_id'])
            db.get(SkillVersion, target['version_id']).dependencies = declarations
            dependencies[path] = declarations
        version.metadata_json = {**version.metadata_json, 'dependencies': dependencies}
    return row


def create(db, actor, body):
    with staging() as root:
        (root / 'SKILL.md').write_text(body['content'], encoding='utf-8')
        return make_resource(db, actor, body['title'], body.get('summary', ''), 'skill', root)


def import_resource(db, actor, archive, title, summary):
    with staging() as root:
        source, _ = extract(archive, root)
        kind = 'skill' if scan(source) == ['.'] else 'package'
        return make_resource(db, actor, title, summary, kind, source, origin='imported')


def fork(db, actor, row, version, title=None, origin='forked'):
    source = version_directory(db, row, version)
    prior = db.get(SkillHubVersion, version.id)
    metadata = copy.deepcopy(prior.metadata_json) if prior else metadata_default(title or row.title, row.summary)
    if row.kind == 'package':
        metadata['_fork_member_ids'] = {location: entry['skill_id'] for location, entry in version.members.items()}
        if not prior:
            metadata['dependencies'] = {location: copy.deepcopy(db.get(SkillVersion, entry['version_id']).dependencies or []) for location, entry in version.members.items()}
    return make_resource(db, actor, title or row.title, row.summary, row.kind, source, origin, row, version, metadata)


def edit(db, actor, identifier, body):
    row = access.owner(db, identifier, actor)
    assert_revision(row, body['revision'])
    if row.archived:
        raise HTTPException(409, '请先恢复归档技能')
    previous = access.native(db, row, row.draft_version_id or row.published_version_id)
    record = db.get(SkillHubVersion, previous.id)
    with staging() as root:
        copy_tree(version_directory(db, row, previous), root)
        operation = body.get('operation', 'write')
        path = relative_path(body['path'])
        target = root.joinpath(*path.parts)
        if operation == 'write':
            if len(body.get('content', '').encode('utf-8')) > 1024 * 1024:
                raise HTTPException(413, '文本编辑上限为1MiB')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body.get('content', ''), encoding='utf-8')
        elif operation == 'delete':
            safe_file(root, body['path']).unlink()
        elif operation == 'rename':
            destination = root.joinpath(*relative_path(body['new_path']).parts)
            if destination.exists():
                raise HTTPException(409, '目标文件已存在')
            destination.parent.mkdir(parents=True, exist_ok=True)
            safe_file(root, body['path']).rename(destination)
        else:
            raise HTTPException(422, '不支持的文件操作')
        if manifest(root) == previous.manifest:
            return row
        freeze(db, row, root, actor.id, record.metadata_json)
    return row


def update_metadata(db, actor, identifier, revision, metadata):
    row = access.owner(db, identifier, actor)
    assert_revision(row, revision)
    if row.archived:
        raise HTTPException(409, '请先恢复归档技能')
    version = access.native(db, row, row.draft_version_id or row.published_version_id)
    freeze(db, row, version_directory(db, row, version), actor.id, metadata)
    row.title, row.summary = metadata['title'], metadata['summary']
    return row


def upload_draft(db, actor, identifier, archive, revision, confirm):
    row = access.owner(db, identifier, actor)
    assert_revision(row, revision)
    if row.archived:
        raise HTTPException(409, '请先恢复归档技能')
    previous = access.native(db, row, row.draft_version_id or row.published_version_id)
    with staging() as root:
        source, _ = extract(archive, root)
        before = {e['path']: e for e in previous.manifest}
        after = {e['path']: e for e in manifest(source)}
        changes = [{'path': p, 'action': 'added' if p not in before else 'deleted' if p not in after else 'modified'}
                   for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
        if confirm:
            freeze(db, row, source, actor.id, db.get(SkillHubVersion, previous.id).metadata_json)
        return {'changes': changes, 'revision': row.revision, 'confirmed': confirm}


def members(db, row, version_id):
    native = access.native(db, row, version_id)
    return [db.get(SkillVersion, v['version_id']) for v in native.members.values()] if row.kind == 'package' else [native]


def test(db, actor, identifier, script=None, sample_input='', scripts=None):
    row = access.owner(db, identifier, actor)
    if row.archived:
        raise HTTPException(409, '请先恢复归档技能')
    record = db.get(SkillHubVersion, row.draft_version_id)
    if not record or record.state != 'draft':
        raise HTTPException(409, '请先创建草稿')
    reports = []
    if not _TEST_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "隔离测试繁忙，请稍后重试")
    try:
        native = access.native(db, row, record.id)
        scan(version_directory(db, row, native))
        package_members = native.members if row.kind == 'package' else {}
        relative_script = script
        if script and row.kind == 'package':
            for location in sorted(package_members, key=len, reverse=True):
                prefix = location + '/'
                if location != '.' and script.startswith(prefix):
                    relative_script = script[len(prefix):]
                    break
        for member in members(db, row, record.id):
            location = next((p for p, entry in package_members.items() if entry['skill_id'] == member.skill_id), '.')
            chosen = (scripts or {}).get(location, relative_script)
            if chosen and row.kind == 'package' and chosen not in {entry['path'] for entry in member.manifest}:
                chosen = None
            allowed_drafts = {m.id for m in members(db, row, record.id)}
            access.check_dependencies(db, member, actor, allowed_drafts=allowed_drafts)
            reports.append(skill_versions.sandbox_smoke_test(db, member.skill_id, member.id,
                chosen, sample_input, commit=False, allowed_drafts=allowed_drafts))
        passed = all(r['status'] == 'passed' or (r['status'] == 'smoke_passed' and not member.dependencies) for r, member in zip(reports, members(db, row, record.id)))
        record.test_status = 'passed' if passed else 'failed'
        record.test_report = {'integrity': 'passed', 'dependencies': 'passed', 'sandbox': reports,
            'warning': '隔离测试不调用真实模型或工具，不保证业务正确性或不存在所有安全风险'}
    except HTTPException as error:
        record.test_status = 'failed'
        record.test_report = {'error': error.detail}
    finally:
        _TEST_SLOTS.release()
    return record


def publish(db, actor, identifier, revision, version_id=None):
    row = access.owner(db, identifier, actor)
    assert_revision(row, revision)
    if row.archived:
        raise HTTPException(409, '请先恢复归档技能')
    identifier = version_id or row.draft_version_id
    if access.is_blocked(db, row, identifier):
        raise HTTPException(409, '不能发布或回滚到安全停用版本')
    record = db.get(SkillHubVersion, identifier)
    if record is None or record.resource_id != row.id:
        raise HTTPException(404, '版本不存在')
    rollback = record.state == 'published'
    if not rollback and identifier != row.draft_version_id:
        raise HTTPException(409, '只能发布当前草稿，历史草稿请先复制为新草稿')
    if not rollback and record.test_status != 'passed':
        raise HTTPException(409, '请先通过检查与隔离测试')
    allowed_drafts = {v.id for v in members(db, row, identifier)}
    for version in members(db, row, identifier):
        access.check_dependencies(db, version, actor, allowed_drafts=allowed_drafts)
        publication = db.get(SkillPublication, version.skill_id)
        skill_versions.publish(db, version.skill_id, version.id, publication.revision if publication else 0,
                               actor.id, rollback=rollback, commit=False, notify_package=False, allowed_drafts=allowed_drafts)
    if row.kind == 'package':
        packages.publish_package(db, db.get(SkillPackage, row.package_id), access.native(db, row, identifier))
        ids = {v.skill_id for v in members(db, row, identifier)}
        for skill in db.query(Skill).filter_by(package_id=row.package_id):
            skill.is_active = skill.id in ids
    record.state = 'published'
    row.published_version_id = identifier
    if row.draft_version_id == identifier:
        row.draft_version_id = None
    row.revision += 1
    db.flush()
    return row
