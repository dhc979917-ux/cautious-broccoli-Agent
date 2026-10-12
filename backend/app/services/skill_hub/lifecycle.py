"""Lifecycle integration preserves source provenance without exposing retained private data."""
from sqlalchemy import inspect, or_
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_governance import SkillVersion, SkillReleaseEvent
from app.models.skill_resource import PackageVersion, ResourceChangeJob
from app.models.skill_hub import (SkillHubResource, SkillHubVersion, SkillHubFork, SkillHubShare,
    SkillMarketplaceListing, SkillMarketplaceRequest, SkillHubInstall, SkillHubReview,
    SkillHubReport, SkillHubBlock)


def detach_user(db, user_id):
    from app.models.skill_evolution import SkillEvolutionJob, SkillEvolutionPreference, SkillEvolutionBinding
    for model in (SkillEvolutionJob, SkillEvolutionBinding, SkillEvolutionPreference):
        if inspect(db.connection()).has_table(model.__tablename__):
            db.query(model).filter_by(user_id=user_id).delete(synchronize_session=False)

    if not inspect(db.connection()).has_table(SkillHubResource.__tablename__):
        return
    for resource in db.query(SkillHubResource).filter_by(owner_id=user_id):
        resource.owner_id = None
        resource.origin = 'retained_private'
        resource.archived = True
        resource.blocked = True
    listings = db.query(SkillMarketplaceListing).filter_by(author_id=user_id).all()
    listing_ids = [r.id for r in listings]
    if listing_ids:
        db.query(SkillHubReview).filter(SkillHubReview.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(SkillHubReport).filter(SkillHubReport.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(SkillHubInstall).filter(SkillHubInstall.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(SkillMarketplaceRequest).filter(SkillMarketplaceRequest.listing_id.in_(listing_ids)).delete(synchronize_session=False)
        db.query(SkillMarketplaceListing).filter(SkillMarketplaceListing.id.in_(listing_ids)).delete(synchronize_session=False)
    db.query(SkillHubShare).filter(or_(SkillHubShare.shared_by == user_id, SkillHubShare.shared_to == user_id)).delete(synchronize_session=False)
    db.query(SkillHubInstall).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(SkillHubReview).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.query(SkillHubReport).filter_by(user_id=user_id).delete(synchronize_session=False)
    for model, field in [(SkillHubVersion, 'created_by'), (SkillVersion, 'created_by'), (SkillVersion, 'owner_id'),
        (Skill, 'created_by'), (SkillPackage, 'created_by'), (PackageVersion, 'created_by'),
        (ResourceChangeJob, 'actor_id'), (SkillReleaseEvent, 'actor_id'),
        (SkillMarketplaceRequest, 'reviewer_id'), (SkillHubReport, 'reviewer_id'), (SkillHubBlock, 'actor_id')]:
        db.query(model).filter(getattr(model, field) == user_id).update({field: None}, synchronize_session=False)
    db.query(SkillHubFork).filter_by(forked_by=user_id).delete(synchronize_session=False)
