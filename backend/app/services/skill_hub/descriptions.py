"""Stable descriptions; never expose private drafts through community endpoints."""
from sqlalchemy import func
from pathlib import PurePosixPath
import yaml
from app.models.user import User
from app.models.skill_hub import (SkillHubResource, SkillHubVersion, SkillHubShare,
    SkillMarketplaceListing, SkillMarketplaceRequest, SkillHubInstall, SkillHubReview)
from app.services.skill_hub import access
from app.services.skill_hub.content import version_directory


def version(db, value):
    return {'id': value.id, 'number': value.number, 'state': value.state, 'test_status': value.test_status,
            'test_report': value.test_report, 'metadata': value.metadata_json, 'created_at': value.created_at}


def resource(db, row, actor, full=False):
    result = {'id': row.id, 'kind': row.kind, 'title': row.title, 'summary': row.summary,
        'owner_id': row.owner_id, 'origin': row.origin, 'allow_fork': row.allow_fork,
        'archived': row.archived, 'blocked': row.blocked, 'revision': row.revision,
        'published_version_id': access.current_id(db, row), 'source_title': row.source_title,
        'source_resource_id': row.source_resource_id, 'source_version_id': row.source_version_id,
        'created_at': row.created_at}
    user = db.get(User, row.owner_id) if row.owner_id else None
    result['author'] = user.username if user else '平台维护'
    if row.owner_id == actor.id:
        result['draft_version_id'] = row.draft_version_id
        result['versions'] = [version(db, v) for v in db.query(SkillHubVersion).filter_by(resource_id=row.id).order_by(SkillHubVersion.number.desc())]
        listing = db.query(SkillMarketplaceListing).filter_by(resource_id=row.id).first()
        result['listing_id'] = listing.id if listing else None
        result['listing_status'] = listing.status if listing else None
        result['share_count'] = sum(access.active_share(s) for s in db.query(SkillHubShare).filter_by(resource_id=row.id))
        install = db.query(SkillHubInstall).filter_by(resource_id=row.id).first()
        upstream = db.get(SkillMarketplaceListing, install.listing_id) if install else None
        result['installed_from'] = install.listing_id if install else None
        result['update_available'] = bool(upstream and upstream.status == 'published' and upstream.version_id != install.installed_version_id)
    else:
        # Resolve only the explicitly shared or system-visible version.
        selected = access.visible_version(db, row, actor)
        result['published_version_id'] = selected.id
        result['versions'] = []
        if row.owner_id:
            share = next(s for s in access.shares_for(db, actor) if s.resource_id == row.id)
            result['share_id'] = share.id
            result['allow_fork'] = share.allow_fork
    if full:
        selected = access.visible_version(db, row, actor)
        result['files'] = selected.manifest
        result['selected_version_id'] = selected.id
        result['can_preview'] = access.preview_allowed(db, row, actor)
        result['members'] = package_members(db, row, selected)
        record = db.get(SkillHubVersion, selected.id)
        result['metadata'] = record.metadata_json if record else {}
        # Frozen origin labels, never source private content or drafts.
        result['provenance'] = []
        current, seen = row, {row.id}
        while current.source_resource_id and current.source_resource_id not in seen and len(seen) <= 16:
            result['provenance'].append({'title': current.source_title or '已移除的来源', 'version_id': current.source_version_id})
            seen.add(current.source_resource_id)
            current = db.get(SkillHubResource, current.source_resource_id)
            if current is None:
                break
        if row.owner_id != actor.id:
            result['revision'] = 0
    return result


def share(db, row):
    target, author = db.get(User, row.shared_to), db.get(User, row.shared_by)
    resource_row = db.get(SkillHubResource, row.resource_id)
    return {'id': row.id, 'resource_id': row.resource_id, 'title': resource_row.title,
            'shared_to': row.shared_to, 'recipient': target.username if target else '已停用用户',
            'shared_by': row.shared_by, 'author': author.username if author else '已停用用户',
            'version_id': row.version_id, 'proposed_version_id': row.proposed_version_id,
            'allow_fork': row.allow_fork, 'expires_at': row.expires_at, 'active': access.active_share(row),
            'revoked_at': row.revoked_at}


def listing(db, row, actor, full=False):
    author = db.get(User, row.author_id)
    resource_row = db.get(SkillHubResource, row.resource_id)
    count = db.query(func.count(func.distinct(SkillHubInstall.user_id))).filter_by(listing_id=row.id).scalar()
    rating, rating_count = db.query(func.avg(SkillHubReview.rating), func.count(SkillHubReview.id)).filter_by(listing_id=row.id, hidden=False).one()
    installs = db.query(SkillHubInstall).filter_by(listing_id=row.id, user_id=actor.id).filter(SkillHubInstall.uninstalled_at.is_(None)).all()
    result = {'id': row.id, 'resource_id': row.resource_id, 'kind': resource_row.kind,
        'author_id': row.author_id, 'author': author.username if author else '已停用用户',
        'status': row.status, 'version_id': row.version_id, 'details': row.details,
        'can_preview': bool(resource_row.allow_fork),
        'rating_avg': round(float(rating or 0), 2), 'rating_count': rating_count,
        'install_count': count, 'installed_resource_id': installs[0].resource_id if installs else None,
        'published_at': row.published_at, 'review_note': row.review_note}
    if full:
        current = access.native(db, resource_row, row.version_id)
        result['files'] = current.manifest
        result['members'] = package_members(db, resource_row, current)
        record = db.get(SkillHubVersion, current.id)
        result['test_report'] = record.test_report if record else {}
        result['history'] = [{'version_id': r.version_id, 'details': r.details, 'reviewed_at': r.reviewed_at}
            for r in db.query(SkillMarketplaceRequest).filter_by(listing_id=row.id, status='approved').order_by(SkillMarketplaceRequest.created_at.desc())]
        result['source_title'] = resource_row.source_title
    return result


def request(db, row):
    listing_row = db.get(SkillMarketplaceListing, row.listing_id)
    resource_row = db.get(SkillHubResource, listing_row.resource_id)
    author = db.get(User, row.submitted_by)
    native = access.native(db, resource_row, row.version_id)
    record = db.get(SkillHubVersion, native.id)
    return {'id': row.id, 'listing_id': row.listing_id, 'resource_id': resource_row.id,
            'version_id': row.version_id, 'kind': resource_row.kind, 'details': row.details,
            'author': author.username if author else '已停用用户', 'author_id': row.submitted_by,
            'status': row.status, 'review_note': row.review_note, 'files': native.manifest,
            'test_report': record.test_report if record else {}, 'created_at': row.created_at,
            'current_version_id': listing_row.version_id}


def package_members(db, row, selected):
    if row.kind != 'package':
        return []
    root = version_directory(db, row, selected)
    result = []
    for location, member in sorted((selected.members or {}).items()):
        path = 'SKILL.md' if location == '.' else location + '/SKILL.md'
        document = root / path
        info = {}
        if document.is_file():
            text = document.read_text(encoding='utf-8', errors='replace')[:32768]
            if text.startswith('---'):
                parts = text.split('---', 2)
                if len(parts) == 3:
                    try:
                        parsed = yaml.safe_load(parts[1])
                        info = parsed if isinstance(parsed, dict) else {}
                    except yaml.YAMLError:
                        pass
        title = info.get('display_name_zh') or info.get('display_name') or info.get('name') or PurePosixPath(location).name or row.title
        summary = info.get('description_zh') or info.get('description') or ''
        result.append({'path': location, 'skill_id': member['skill_id'], 'version_id': member['version_id'],
                       'title': str(title)[:200], 'summary': str(summary)[:2000]})
    return result
