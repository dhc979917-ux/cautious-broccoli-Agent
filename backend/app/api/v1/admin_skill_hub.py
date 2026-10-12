"""Explicit community moderation; private content is not browsable by admin role."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.deps import require_admin
from app.models.user import User
from app.models.skill_hub import SkillHubResource, SkillMarketplaceRequest, SkillMarketplaceListing, SkillHubReport, SkillHubReview
from app.schemas.skill_hub import Decision, Note, Block, Flag
from app.services.skill_hub import access, content, sharing, marketplace, descriptions
from app.services.skill_resource_files import preview
from app.api.v1.skill_hub import enabled, skill_diff

router = APIRouter(prefix='/admin/skill-hub', tags=['技能社区审核'], dependencies=[Depends(enabled)])


@router.get('/requests')
def requests(status: str = 'pending_review', actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    query = db.query(SkillMarketplaceRequest)
    if status:
        query = query.filter_by(status=status)
    return [descriptions.request(db, r) for r in query.order_by(SkillMarketplaceRequest.created_at.desc())]


@router.get('/requests/{identifier}/file')
def file(identifier: str, path: str, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    request = db.get(SkillMarketplaceRequest, identifier)
    if not request:
        raise HTTPException(404, '申请不存在')
    listing = db.get(SkillMarketplaceListing, request.listing_id)
    row = access.resource(db, listing.resource_id)
    return preview(content.version_directory(db, row, access.native(db, row, request.version_id)), path)


@router.get('/requests/{identifier}/diff')
def diff(identifier: str, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    request = db.get(SkillMarketplaceRequest, identifier)
    if not request:
        raise HTTPException(404, '申请不存在')
    listing = db.get(SkillMarketplaceListing, request.listing_id)
    row = access.resource(db, listing.resource_id)
    before = access.native(db, row, listing.version_id).manifest if listing.version_id else []
    return skill_diff(before, access.native(db, row, request.version_id).manifest)


@router.post('/requests/{identifier}/decision')
def decide(identifier: str, body: Decision, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    record = marketplace.decide(db, actor, identifier, body.approved, body.note)
    db.commit()
    return descriptions.request(db, record)


@router.get('/listings')
def listings(actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [descriptions.listing(db, row, actor) for row in db.query(SkillMarketplaceListing).order_by(SkillMarketplaceListing.updated_at.desc())]


@router.post('/listings/{identifier}/offline')
def offline(identifier: str, body: Note, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = marketplace.offline(db, actor, identifier, body.note)
    db.commit()
    return {'id': row.id, 'status': row.status}


@router.post('/resources/{identifier}/block')
def block(identifier: str, body: Block, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = marketplace.block(db, actor, identifier, body.version_id, body.note, body.descendants)
    db.commit()
    return result


@router.get('/resources')
def resources(actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [{'id': r.id, 'kind': r.kind, 'title': r.title, 'owner_id': r.owner_id, 'origin': r.origin,
        'allow_fork': r.allow_fork, 'blocked': r.blocked, 'archived': r.archived,
        'published_version_id': access.current_id(db, r)} for r in db.query(SkillHubResource)]


@router.put('/resources/{identifier}/fork-policy')
def fork_policy(identifier: str, body: Flag, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = access.resource(db, identifier, lock=True)
    if row.owner_id is not None:
        raise HTTPException(403, '仅平台维护资源可配置系统派生策略')
    row.allow_fork = body.value
    sharing.audit(db, actor, 'fork_policy', row.id, {'allow_fork': body.value})
    db.commit()
    return {'allow_fork': row.allow_fork}


@router.get('/reports')
def reports(actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [{'id': r.id, 'listing_id': r.listing_id, 'reason': r.reason, 'status': r.status,
        'resolution': r.resolution, 'created_at': r.created_at} for r in db.query(SkillHubReport).order_by(SkillHubReport.created_at.desc())]


@router.post('/reports/{identifier}/resolve')
def resolve(identifier: str, body: Note, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(SkillHubReport, identifier)
    if not row:
        raise HTTPException(404, '举报不存在')
    row.status, row.resolution, row.reviewer_id = 'resolved', body.note, actor.id
    sharing.audit(db, actor, 'report_resolve', identifier)
    db.commit()
    return {'resolved': True}


@router.put('/reviews/{identifier}/hidden')
def hide_review(identifier: str, body: Flag, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(SkillHubReview, identifier)
    if not row:
        raise HTTPException(404, '评价不存在')
    row.hidden = body.value
    sharing.audit(db, actor, 'review_moderate', row.id, {'hidden': body.value})
    db.commit()
    return {'hidden': row.hidden}

@router.get('/reviews')
def reviews(actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [{'id': r.id, 'listing_id': r.listing_id, 'rating': r.rating, 'comment': r.comment,
             'hidden': r.hidden, 'created_at': r.created_at}
            for r in db.query(SkillHubReview).order_by(SkillHubReview.created_at.desc())]
