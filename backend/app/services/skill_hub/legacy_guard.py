"""Prevent legacy system-management routes bypassing personal-resource privacy."""
import re
from fastapi import HTTPException, Request, Depends
from app.database import get_db
from app.models.skill_hub import SkillHubResource
from app.services.skill_hub.access import managed_map


def require_system_resource(request: Request, db=Depends(get_db)):
    path = request.url.path
    match = re.search(r'/(?:admin/skills|skills)/(\d+)(?:/|$)', path)
    package = re.search(r'/skills/packages/(\d+)(?:/|$)', path)
    generic = re.search(r'/skill-resources/(skill|package)/(\d+)(?:/|$)', path)
    kind, identifier = ('skill', int(match.group(1))) if match else ('package', int(package.group(1))) if package else (generic.group(1), int(generic.group(2))) if generic else (None, None)
    if identifier is None:
        return
    private = identifier in managed_map(db) if kind == 'skill' else db.query(SkillHubResource.id).filter_by(package_id=identifier).filter((SkillHubResource.owner_id.isnot(None)) | (SkillHubResource.origin == 'retained_private')).first()
    if private:
        raise HTTPException(404, '该资源只能通过个人技能入口访问')
