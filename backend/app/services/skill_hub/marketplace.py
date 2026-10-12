"""Community publication requests are immutable and independent from personal releases."""
import copy
from uuid import uuid4
from fastapi import HTTPException
from app.models.user import User
from app.models.skill_hub import (SkillHubResource, SkillHubVersion, SkillMarketplaceListing,
    SkillMarketplaceRequest, SkillHubInstall, SkillHubReview, SkillHubReport, SkillHubBlock)
from app.services.skill_hub import access, content, sharing


def published_listing(db, identifier):
    row = db.query(SkillMarketplaceListing).filter_by(id=identifier, status='published').one_or_none()
    if row is None or access.is_blocked(db, access.resource(db, row.resource_id), row.version_id):
        raise HTTPException(404, '社区技能不存在或已下架')
    return row


def submit(db, actor, identifier, details):
    row = access.owner(db, identifier, actor)
    if row.archived or not row.published_version_id:
        raise HTTPException(409, '请先发布自用版本')
    record = db.get(SkillHubVersion, row.published_version_id)
    if not record or record.test_status != 'passed':
        raise HTTPException(409, '社区投稿必须通过当前版本检查与测试')
    if not details.get('license', '').strip() or not details.get('example', '').strip():
        raise HTTPException(422, '请填写许可说明和使用示例')
    for version in content.members(db, row, row.published_version_id):
        access.check_dependencies(db, version, actor)
        for dep in version.dependencies or []:
            if dep['kind'] == 'skill':
                dependency_row = access.managed_map(db).get(dep['skill_id'])
                if dependency_row and dependency_row.id != row.id:
                    raise HTTPException(409, '依赖其他个人技能的资源不能直接社区上架，请先整理成完整技能包')
    listing = db.query(SkillMarketplaceListing).filter_by(resource_id=row.id).with_for_update().first()
    if listing is None:
        listing = SkillMarketplaceListing(id=str(uuid4()), resource_id=row.id, author_id=actor.id)
        db.add(listing); db.flush()
    if db.query(SkillMarketplaceRequest).filter_by(listing_id=listing.id, status='pending_review').first():
        raise HTTPException(409, '已有待审申请，请先等待结果或撤回')
    request = SkillMarketplaceRequest(id=str(uuid4()), listing_id=listing.id,
        version_id=row.published_version_id, submitted_by=actor.id, details=copy.deepcopy(details))
    db.add(request)
    if listing.status != 'published':
        listing.status = 'pending_review'
    sharing.audit(db, actor, 'submit', listing.id, {'version_id': request.version_id})
    return request


def withdraw(db, actor, identifier):
    request = db.query(SkillMarketplaceRequest).filter_by(id=identifier, submitted_by=actor.id).with_for_update().one_or_none()
    if request is None or request.status != 'pending_review':
        raise HTTPException(404, '待审申请不存在')
    request.status = 'withdrawn'
    listing = db.get(SkillMarketplaceListing, request.listing_id)
    if not listing.version_id:
        listing.status = 'draft'
    sharing.audit(db, actor, 'withdraw', request.id)
    return request


def decide(db, actor, identifier, approved, note):
    request = db.query(SkillMarketplaceRequest).filter_by(id=identifier).with_for_update().one_or_none()
    if request is None or request.status != 'pending_review':
        raise HTTPException(409, '申请不存在或已处理')
    if request.submitted_by == actor.id:
        raise HTTPException(403, '不能审核自己的投稿')
    if not approved and not note.strip():
        raise HTTPException(422, '驳回必须填写原因')
    listing = db.query(SkillMarketplaceListing).filter_by(id=request.listing_id).with_for_update().one()
    row = access.resource(db, listing.resource_id)
    if approved and (row.archived or access.is_blocked(db, row, request.version_id)):
        raise HTTPException(409, '资源已归档或安全停用，不能上架')
    if approved:
        if not db.get(SkillHubVersion, request.version_id) or db.get(SkillHubVersion, request.version_id).test_status != 'passed':
            raise HTTPException(409, '提交版本的测试证明不可用')
        actor_user = db.get(User, request.submitted_by)
        for member in content.members(db, row, request.version_id):
            access.check_dependencies(db, member, actor_user)
        content.scan(content.version_directory(db, row, access.native(db, row, request.version_id)))
        listing.version_id = request.version_id
        listing.details = copy.deepcopy(request.details)
        listing.status = 'published'
        listing.published_at = access.utcnow()
        for install in db.query(SkillHubInstall).filter_by(listing_id=listing.id).filter(SkillHubInstall.uninstalled_at.is_(None)):
            if install.installed_version_id != request.version_id:
                sharing.notify(db, install.user_id, '已安装技能有新版本', '新版需主动确认升级；个人修改不会自动被覆盖。')
    elif listing.status != 'published':
        listing.status = 'rejected'
    request.status = 'approved' if approved else 'rejected'
    request.reviewer_id = actor.id
    request.review_note = note
    request.reviewed_at = access.utcnow()
    listing.review_note = note
    listing.updated_at = access.utcnow()
    sharing.notify(db, request.submitted_by, '技能社区审核'+('通过' if approved else '驳回'), note)
    sharing.audit(db, actor, request.status, request.id, {'version_id': request.version_id})
    return request


def install(db, actor, identifier, another=False):
    from app.models.user import User
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    listing = published_listing(db, identifier)
    # Listing row lock serializes retries and installations against approval changes.
    listing = db.query(SkillMarketplaceListing).filter_by(id=listing.id).with_for_update().one()
    existing = db.query(SkillHubInstall).filter_by(listing_id=listing.id, user_id=actor.id).filter(SkillHubInstall.uninstalled_at.is_(None)).first()
    if existing and not another:
        return access.resource(db, existing.resource_id), existing
    source = access.resource(db, listing.resource_id)
    source_version = access.native(db, source, listing.version_id)
    row = content.fork(db, actor, source, source_version, origin='installed')
    record = SkillHubInstall(id=str(uuid4()), listing_id=listing.id, user_id=actor.id,
        resource_id=row.id, installed_version_id=listing.version_id)
    db.add(record)
    try:
        content.test(db, actor, row.id)
        if db.get(SkillHubVersion, row.draft_version_id).test_status != 'passed':
            raise HTTPException(409, '安装检查未通过，请配置依赖后重试')
        content.publish(db, actor, row.id, row.revision)
        record.local_version_id = row.published_version_id
    except HTTPException as error:
        # Installation remains an explicit draft, never an unvalidated usable version.
        draft = db.get(SkillHubVersion, row.draft_version_id)
        draft.test_status = 'failed'
        draft.test_report = {'error': error.detail, 'install_pending_configuration': True}
    sharing.audit(db, actor, 'install', row.id, {'listing_id': listing.id})
    return row, record


def upgrade(db, actor, identifier):
    row = access.owner(db, identifier, actor)
    record = db.query(SkillHubInstall).filter_by(resource_id=row.id, user_id=actor.id).with_for_update().first()
    if not record or record.uninstalled_at:
        raise HTTPException(404, '安装记录不存在')
    listing = published_listing(db, record.listing_id)
    if listing.version_id == record.installed_version_id:
        return row, 'already_current'
    baseline = access.native(db, row, record.local_version_id) if record.local_version_id else None
    current = access.native(db, row, row.draft_version_id or row.published_version_id)
    if baseline is None or baseline.content_hash != current.content_hash or row.draft_version_id:
        new_row, _ = install(db, actor, listing.id, another=True)
        return new_row, 'new_copy'
    source = access.resource(db, listing.resource_id)
    source_version = access.native(db, source, listing.version_id)
    upstream = db.get(SkillHubVersion, listing.version_id)
    metadata = copy.deepcopy(upstream.metadata_json)
    if row.kind == 'package':
        metadata['_fork_member_ids'] = {location: entry['skill_id'] for location, entry in source_version.members.items()}
    content.freeze(db, row, content.version_directory(db, source, source_version), actor.id, metadata)
    checked = content.test(db, actor, row.id)
    if checked.test_status != 'passed':
        raise HTTPException(409, '新版检查失败，当前自用版本未改变')
    content.publish(db, actor, row.id, row.revision)
    record.installed_version_id = listing.version_id
    record.local_version_id = row.published_version_id
    row.source_version_id = listing.version_id
    sharing.audit(db, actor, 'upgrade', row.id)
    return row, 'upgraded'


def offline(db, actor, identifier, note):
    listing = db.query(SkillMarketplaceListing).filter_by(id=identifier).with_for_update().one_or_none()
    if listing is None:
        raise HTTPException(404, '条目不存在')
    if not note.strip():
        raise HTTPException(422, '请填写下架原因')
    listing.status = 'offline'
    listing.review_note = note
    for install in db.query(SkillHubInstall).filter_by(listing_id=listing.id):
        sharing.notify(db, install.user_id, '技能来源已下架', note+'；已有个人副本保留。')
    sharing.audit(db, actor, 'offline', listing.id, {'reason': note})
    return listing


def block(db, actor, identifier, version_id, note, descendants=False):
    row = access.resource(db, identifier, lock=True)
    if not note.strip():
        raise HTTPException(422, '请填写安全停用原因')
    if version_id:
        access.native(db, row, version_id)
    rows = [row]
    if descendants:
        cursor = [row]
        visited = {row.id}
        while cursor:
            parent = cursor.pop()
            for child in db.query(SkillHubResource).filter_by(source_resource_id=parent.id):
                if child.id not in visited and (parent != row or not version_id or child.source_version_id == version_id):
                    visited.add(child.id); rows.append(child); cursor.append(child)
    for affected in rows:
        db.add(SkillHubBlock(resource_id=affected.id,
            version_id=version_id if affected.id == row.id else None, actor_id=actor.id, reason=note))
        if affected.id != row.id or not version_id:
            affected.blocked = True
        for listing in db.query(SkillMarketplaceListing).filter_by(resource_id=affected.id):
            if not version_id or listing.version_id == version_id or affected.id != row.id:
                listing.status = 'offline'; listing.review_note = note
        if affected.owner_id:
            sharing.notify(db, affected.owner_id, '技能已被安全停用', note)
    sharing.audit(db, actor, 'block', row.id, {'version_id': version_id, 'descendants': descendants, 'reason': note})
    return {'affected_resources': len(rows)}


def review(db, actor, identifier, rating, comment):
    listing = published_listing(db, identifier)
    if listing.author_id == actor.id:
        raise HTTPException(403, '作者不能给自己的条目评分')
    install = db.query(SkillHubInstall).filter_by(listing_id=listing.id, user_id=actor.id).order_by(SkillHubInstall.created_at.desc()).first()
    if not install:
        raise HTTPException(403, '安装过该技能后才能评价')
    record = db.query(SkillHubReview).filter_by(listing_id=listing.id, user_id=actor.id).with_for_update().first()
    if record is None:
        record = SkillHubReview(id=str(uuid4()), listing_id=listing.id, user_id=actor.id)
        db.add(record)
    record.rating, record.comment, record.version_id = rating, comment, install.installed_version_id
    sharing.audit(db, actor, 'review', listing.id)
    return record
