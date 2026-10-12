"""Skill Hub user APIs: independent resources, immutable versions and bounded sharing."""
import shutil
import json
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.models.skill_hub import (SkillHubResource, SkillHubVersion, SkillHubShare,
    SkillMarketplaceListing, SkillMarketplaceRequest, SkillHubInstall, SkillHubReview, SkillHubReport)
from app.schemas.skill_hub import Create, Fork, Edit, Metadata, Release, Test, Share, Details, Review, Note, Flag
from app.services.skill_hub import access, content, sharing, marketplace, descriptions
from app.services.skill_resource_files import preview, safe_file, download_type


def enabled():
    if not settings.SKILL_HUB_ENABLED:
        raise HTTPException(404, '技能个人空间尚未开放')


router = APIRouter(prefix='/skill-hub', tags=['技能中心与个人技能'], dependencies=[Depends(enabled)])


def commit(db, actor, action, row):
    sharing.audit(db, actor, action, row.id)
    db.commit()
    return descriptions.resource(db, row, actor)


@router.get('/system')
def system(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.skill_hub.registry import register_system
    register_system(db)
    db.commit()
    result = []
    for row in db.query(SkillHubResource).filter(SkillHubResource.owner_id.is_(None), SkillHubResource.origin == 'system'):
        try:
            result.append(descriptions.resource(db, row, actor))
        except HTTPException:
            continue
    return result


@router.get('/users')
def recipients(keyword: str = '', actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(User).filter(User.is_active.is_(True), User.id != actor.id)
    if keyword:
        query = query.filter(User.username.ilike('%'+keyword[:80]+'%'))
    return [{'id': u.id, 'name': u.username} for u in query.order_by(User.id).limit(50)]


@router.get('/my')
def mine(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [descriptions.resource(db, row, actor) for row in db.query(SkillHubResource).filter_by(owner_id=actor.id).order_by(SkillHubResource.created_at.desc())]


@router.get('/shares')
def shares(incoming: bool = False, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(SkillHubShare).filter_by(**{'shared_to' if incoming else 'shared_by': actor.id})
    return [descriptions.share(db, s) for s in query.order_by(SkillHubShare.created_at.desc())]


@router.get('/publications')
def publications(actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(SkillMarketplaceRequest).filter_by(submitted_by=actor.id).order_by(SkillMarketplaceRequest.created_at.desc())
    return [descriptions.request(db, r) for r in rows]


@router.post('/resources', status_code=201)
def create(body: Create, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = content.create(db, actor, body.model_dump())
    return commit(db, actor, 'create', row)


@router.post('/import', status_code=201)
def import_resource(file: UploadFile = File(...), title: str = Form(...), summary: str = Form(''),
                    actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not title.strip() or len(title) > 200:
        raise HTTPException(422, '标题不能为空或超过200字')
    with content.staging() as root:
        archive = root / 'upload.zip'
        with archive.open('wb') as stream:
            total = 0
            while data := file.file.read(1024 * 1024):
                total += len(data)
                if total > 100 * 1024 * 1024:
                    raise HTTPException(413, '导入包超过100MiB')
                stream.write(data)
        row = content.import_resource(db, actor, archive, title.strip(), summary[:2000])
    return commit(db, actor, 'import', row)


@router.post('/import-folder', status_code=201)
def import_folder(files: list[UploadFile] = File(...), paths: str = Form(...), title: str = Form(...),
                  summary: str = Form(''), actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.skill_resource_files import relative_path
    if not title.strip() or len(title) > 200 or not 1 <= len(files) <= 2000:
        raise HTTPException(422, '名称或文件数量不符合要求')
    try:
        names = json.loads(paths)
    except (ValueError, TypeError):
        raise HTTPException(422, '文件路径列表格式无效')
    if not isinstance(names, list) or len(names) != len(files) or any(not isinstance(n, str) for n in names):
        raise HTTPException(422, '文件和路径必须逐项对应')
    with content.staging() as root:
        seen, total = set(), 0
        for upload, name in zip(files, names):
            relative = relative_path(name)
            if relative.as_posix() in seen:
                raise HTTPException(422, '文件夹中存在重复路径')
            seen.add(relative.as_posix())
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('wb') as out:
                while data := upload.file.read(1024 * 1024):
                    total += len(data)
                    if total > 100 * 1024 * 1024:
                        raise HTTPException(413, '文件夹超过100MiB')
                    out.write(data)
        children = list(root.iterdir())
        source = children[0] if len(children) == 1 and children[0].is_dir() else root
        locations = content.scan(source)
        row = content.make_resource(db, actor, title.strip(), summary[:2000],
            'skill' if locations == ['.'] else 'package', source, 'imported')
    return commit(db, actor, 'import_folder', row)


@router.get('/resources/{identifier}/trial')
def trial(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier, actor, own=True)
    if not row.published_version_id or row.archived:
        raise HTTPException(409, '请先发布可用的自用版本；草稿不能进入真实业务对话')
    if access.is_blocked(db, row, row.published_version_id):
        raise HTTPException(409, '该技能版本已停用')
    versions = content.members(db, row, row.published_version_id)
    for version in versions:
        access.check_dependencies(db, version, actor)
    from app.models.skill import Skill
    return {'version_id': row.published_version_id,
            'skills': [db.get(Skill, v.skill_id).name for v in versions]}


@router.get('/resources/{identifier}')
def detail(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    return descriptions.resource(db, row, actor, full=True)


@router.post('/resources/{identifier}/fork', status_code=201)
def fork(identifier: str, body: Fork, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    version = access.visible_version(db, row, actor, body.version_id)
    if row.owner_id == actor.id:
        allowed = True
    elif row.owner_id is None:
        allowed = row.allow_fork
    else:
        shared = next(s for s in access.shares_for(db, actor) if s.resource_id == row.id)
        allowed = shared.allow_fork
    if not allowed:
        raise HTTPException(403, '该资源允许使用但未允许派生')
    return commit(db, actor, 'fork', content.fork(db, actor, row, version, body.title))


@router.post('/resources/{identifier}/files')
def edit(identifier: str, body: Edit, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return commit(db, actor, 'edit_draft', content.edit(db, actor, identifier, body.model_dump()))


@router.get('/resources/{identifier}/versions/{version_id}/file')
def file(identifier: str, version_id: str, path: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    version = access.visible_version(db, row, actor, version_id)
    access.require_preview(db, row, actor)
    return preview(content.version_directory(db, row, version), path)


@router.get('/resources/{identifier}/versions/{version_id}/download')
def download(identifier: str, version_id: str, path: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    version = access.visible_version(db, row, actor, version_id)
    access.require_preview(db, row, actor)
    target = safe_file(content.version_directory(db, row, version), path)
    return FileResponse(target, filename=target.name, media_type=download_type(target, False),
        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store', 'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.put('/resources/{identifier}/metadata')
def metadata(identifier: str, body: Metadata, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return commit(db, actor, 'metadata', content.update_metadata(db, actor, identifier, body.revision, body.details.model_dump()))


@router.post('/resources/{identifier}/upload')
def upload(identifier: str, revision: int = Form(...), confirm: bool = Form(False), file: UploadFile = File(...),
           actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with content.staging() as root:
        archive = root / 'update.zip'
        total = 0
        with archive.open('wb') as stream:
            while data := file.file.read(1024 * 1024):
                total += len(data)
                if total > 100 * 1024 * 1024:
                    raise HTTPException(413, '更新包超过100MiB')
                stream.write(data)
        result = content.upload_draft(db, actor, identifier, archive, revision, confirm)
    if confirm:
        sharing.audit(db, actor, 'upload_draft', identifier)
        db.commit()
    return result


@router.get('/resources/{identifier}/diff')
def diff(identifier: str, before: str, after: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    first, second = access.visible_version(db, row, actor, before), access.visible_version(db, row, actor, after)
    return skill_diff(first.manifest, second.manifest)


def skill_diff(first, second):
    old, new = {e['path']: e for e in first}, {e['path']: e for e in second}
    return [{'path': p, 'action': 'added' if p not in old else 'deleted' if p not in new else 'modified'}
        for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p)]


@router.post('/resources/{identifier}/test')
def test(identifier: str, body: Test, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = content.test(db, actor, identifier, body.script, body.sample_input, body.scripts)
    sharing.audit(db, actor, 'test', identifier)
    db.commit()
    return descriptions.version(db, record)


@router.post('/resources/{identifier}/publish')
def publish(identifier: str, body: Release, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = content.publish(db, actor, identifier, body.revision, body.version_id)
    return commit(db, actor, 'publish_personal', row)


@router.put('/resources/{identifier}/archive')
def archive(identifier: str, body: Flag, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.owner(db, identifier, actor)
    if body.value and db.query(SkillMarketplaceListing).filter_by(resource_id=row.id, status='published').first():
        raise HTTPException(409, '请先申请下架社区条目，再归档个人资源')
    row.archived = body.value
    return commit(db, actor, 'archive', row)


@router.post('/resources/{identifier}/share')
def share(identifier: str, body: Share, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = sharing.share(db, actor, identifier, body.model_dump())
    db.commit()
    return descriptions.share(db, record)


@router.delete('/shares/{identifier}')
def revoke(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = sharing.revoke(db, actor, identifier)
    db.commit()
    return descriptions.share(db, record)


@router.post('/shares/{identifier}/accept')
def accept(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = sharing.accept(db, actor, identifier)
    db.commit()
    return descriptions.share(db, record)


@router.post('/resources/{identifier}/submit', status_code=201)
def submit(identifier: str, body: Details, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = marketplace.submit(db, actor, identifier, body.model_dump())
    db.commit()
    return descriptions.request(db, record)


@router.post('/requests/{identifier}/withdraw')
def withdraw(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = marketplace.withdraw(db, actor, identifier)
    db.commit()
    return descriptions.request(db, record)


@router.get('/marketplace')
def listings(keyword: str = '', category: str = '', tag: str = '', sort: str = 'latest',
             actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = []
    for row in db.query(SkillMarketplaceListing).filter_by(status='published').order_by(SkillMarketplaceListing.published_at.desc()):
        if access.is_blocked(db, access.resource(db, row.resource_id), row.version_id):
            continue
        details = row.details
        if category and category != details.get('category'):
            continue
        if tag and tag not in details.get('tags', []):
            continue
        if keyword and keyword.casefold() not in ' '.join([details.get('title', ''), details.get('summary', ''), *details.get('tags', [])]).casefold():
            continue
        rows.append(descriptions.listing(db, row, actor))
    if sort == 'popular':
        rows.sort(key=lambda r: r['install_count'], reverse=True)
    elif sort == 'rating':
        rows.sort(key=lambda r: (r['rating_avg'] * r['rating_count'] + 3 * 5) / (r['rating_count'] + 5), reverse=True)
    return rows


@router.get('/categories')
def categories(actor: User = Depends(get_current_user)):
    return ['办公效率', '数据分析', '开发工具', '文档处理', '行业应用', '其他']


@router.get('/marketplace/{identifier}')
def listing(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return descriptions.listing(db, marketplace.published_listing(db, identifier), actor, full=True)


@router.get('/marketplace/{identifier}/file')
def community_file(identifier: str, path: str, version_id: str | None = None, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    listing = marketplace.published_listing(db, identifier)
    row = access.resource(db, listing.resource_id)
    if version_id and version_id != listing.version_id:
        raise HTTPException(409, '社区批准版本已更新，请重新打开详情')
    access.require_preview(db, row, actor, community=True)
    return preview(content.version_directory(db, row, access.native(db, row, listing.version_id)), path)


@router.post('/marketplace/{identifier}/install', status_code=201)
def install(identifier: str, another: bool = False, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row, record = marketplace.install(db, actor, identifier, another)
    db.commit()
    return descriptions.resource(db, row, actor)


@router.post('/resources/{identifier}/upgrade')
def upgrade(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row, mode = marketplace.upgrade(db, actor, identifier)
    db.commit()
    return {'resource': descriptions.resource(db, row, actor), 'mode': mode}


@router.post('/resources/{identifier}/uninstall')
def uninstall(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.owner(db, identifier, actor)
    record = db.query(SkillHubInstall).filter_by(resource_id=row.id, user_id=actor.id).first()
    if record is None:
        raise HTTPException(404, '安装记录不存在')
    record.uninstalled_at = access.utcnow()
    row.archived = True
    return commit(db, actor, 'uninstall', row)


@router.get('/marketplace/{identifier}/reviews')
def reviews(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    marketplace.published_listing(db, identifier)
    return [{'id': r.id, 'user_id': r.user_id, 'author': db.get(User, r.user_id).username,
        'rating': r.rating, 'comment': r.comment, 'version_id': r.version_id,
        'author_reply': r.author_reply, 'created_at': r.created_at}
        for r in db.query(SkillHubReview).filter_by(listing_id=identifier, hidden=False).order_by(SkillHubReview.created_at.desc())]


@router.post('/marketplace/{identifier}/reviews')
def review(identifier: str, body: Review, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = marketplace.review(db, actor, identifier, body.rating, body.comment)
    db.commit()
    return {'id': record.id}


@router.delete('/reviews/{identifier}')
def delete_review(identifier: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = db.query(SkillHubReview).filter_by(id=identifier, user_id=actor.id).first()
    if not record:
        raise HTTPException(404, '评价不存在')
    db.delete(record); db.commit()
    return {'deleted': True}


@router.post('/reviews/{identifier}/reply')
def reply(identifier: str, body: Note, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    record = db.get(SkillHubReview, identifier)
    if not record or db.get(SkillMarketplaceListing, record.listing_id).author_id != actor.id:
        raise HTTPException(404, '评价不存在')
    record.author_reply = body.note
    db.commit()
    return {'updated': True}


@router.post('/marketplace/{identifier}/report', status_code=201)
def report(identifier: str, body: Note, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    marketplace.published_listing(db, identifier)
    record = SkillHubReport(id=str(uuid4()), listing_id=identifier, user_id=actor.id, reason=body.note)
    db.add(record); db.commit()
    return {'id': record.id}


@router.get('/resources/{identifier}/versions/{version_id}/files')
def version_files(identifier: str, version_id: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.resource(db, identifier)
    return access.visible_version(db, row, actor, version_id).manifest


@router.post('/resources/{identifier}/offline')
def author_offline(identifier: str, body: Note, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = access.owner(db, identifier, actor)
    listing = db.query(SkillMarketplaceListing).filter_by(resource_id=row.id).first()
    if not listing:
        raise HTTPException(404, '社区条目不存在')
    marketplace.offline(db, actor, listing.id, body.note)
    db.commit()
    return {'status': 'offline'}
