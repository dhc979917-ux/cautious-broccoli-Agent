"""Versioned Skill administration; existing skill routes remain compatible."""

from app.services.skill_hub.legacy_guard import require_system_resource
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, require_admin
from app.database import get_db
from app.models.skill import Skill
from app.models.skill_governance import SkillPublication, SkillReleaseEvent, SkillVersion
from app.models.user import User
from app.services import skill_versions


router = APIRouter(prefix="/admin/skills", tags=["Skill 版本治理"], dependencies=[Depends(require_system_resource)])


@router.post('/{skill_id}/upload-update')
async def upload_update(skill_id: int, request: Request, file: UploadFile = File(...),
                        actor: User = Depends(require_admin)):
    import asyncio
    import tempfile
    from pathlib import Path
    from app.database import SessionLocal
    from app.services.skill_service import SkillBusyError
    from app.services.skill_update import update_from_zip
    from app.services.skill_upload_service import spool_to, SkillUploadError

    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        raise HTTPException(409, '技能版本治理未启用，暂不能上传更新')
    if not (file.filename or '').lower().endswith('.zip'):
        raise HTTPException(422, '请选择包含 SKILL.md 的 ZIP 技能更新包')
    try:
        with tempfile.TemporaryDirectory(prefix='skill-update-') as directory:
            archive = Path(directory) / 'update.zip'
            await asyncio.to_thread(spool_to, file.file, archive)
            def apply_update():
                with SessionLocal() as db:
                    return update_from_zip(db, skill_id, archive, file.filename, actor.id)
            result = await asyncio.to_thread(apply_update)
    except SkillUploadError as error:
        raise HTTPException(error.status_code, error.message)
    except SkillBusyError as error:
        raise HTTPException(409, '技能上传或同步正在进行，请稍后重试') from error
    request.state.audit_changes = {'skill_update': 'published', 'skill_id': skill_id}
    return result


@router.get("/versioning")
def versioning(_: User = Depends(get_current_user)):
    return {"enabled": settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED}


class Dependency(BaseModel):
    kind: Literal["skill", "tool", "model"]
    skill_id: int | None = None
    version_id: str | None = None
    server_id: int | None = None
    tool_name: str | None = None
    schema_digest: str | None = None
    model_id: str | None = None


class DraftUpdate(BaseModel):
    revision: int = Field(ge=0)
    dependencies: list[Dependency] = Field(default_factory=list, max_length=50)
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    owner_id: int | None = Field(default=None, gt=0)


class ReleaseRequest(BaseModel):
    revision: int = Field(ge=0)


class SandboxRequest(BaseModel):
    script: str | None = Field(default=None, max_length=240)
    sample_input: str = Field(default="", max_length=4000)


def describe(version: SkillVersion) -> dict:
    return {"id": version.id, "skill_id": version.skill_id, "status": version.status,
            "content_hash": version.content_hash, "source_rel_path": version.source_rel_path,
            "manifest": version.manifest, "dependencies": version.dependencies,
            "category": version.category, "tags": version.tags or [], "owner_id": version.owner_id,
            "test_status": version.test_status, "test_summary": version.test_summary,
            "revision": version.revision, "created_at": version.created_at}


@router.get("/{skill_id}/versions")
def versions(skill_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill 不存在")
    publication = db.get(SkillPublication, skill_id)
    rows = db.query(SkillVersion).filter_by(skill_id=skill_id).order_by(
        SkillVersion.created_at.desc()).all()
    events = db.query(SkillReleaseEvent).filter_by(skill_id=skill_id).order_by(
        SkillReleaseEvent.id.desc()).limit(50).all()
    return {"skill_id": skill_id, "dir_missing": skill.dir_missing,
            "versioning_enabled": settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED,
            "published_version_id": publication.version_id if publication else None,
            "revision": publication.revision if publication else 0,
            "versions": [describe(row) for row in rows],
            "history": [{"id": event.id, "action": event.action,
                         "previous_version_id": event.previous_version_id,
                         "version_id": event.version_id, "created_at": event.created_at}
                        for event in events]}


@router.post("/{skill_id}/versions")
def candidate(skill_id: int, request: Request,
              actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, "Skill 不存在")
    if not skill_versions.source_changed(db, skill):
        raise HTTPException(409, "文件内容未变化，已有对应快照")
    version = skill_versions.create_snapshot(db, skill_id, actor.id)
    request.state.audit_changes = {"skill_version": "created"}
    return describe(version)


@router.put("/{skill_id}/versions/{version_id}")
def edit_draft(skill_id: int, version_id: str, payload: DraftUpdate, request: Request,
               _: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = skill_versions.revise_draft(
        db, skill_id, version_id, payload.revision,
        dependencies=[dependency.model_dump(exclude_none=True) for dependency in payload.dependencies],
        category=payload.category, tags=payload.tags, owner_id=payload.owner_id,
    )
    request.state.audit_changes = {"skill_version": "revised"}
    return describe(version)


@router.get("/{skill_id}/versions/{version_id}/diff")
def diff(skill_id: int, version_id: str, _: User = Depends(require_admin),
         db: Session = Depends(get_db)):
    version = db.get(SkillVersion, version_id)
    publication = db.get(SkillPublication, skill_id)
    if version is None or version.skill_id != skill_id or publication is None:
        raise HTTPException(404, "Skill 版本或发布记录不存在")
    current = db.get(SkillVersion, publication.version_id)
    return {"from": current.id, "to": version.id,
            "files": skill_versions.file_changes(current, version),
            "dependencies": {"before": current.dependencies, "after": version.dependencies}}


@router.post("/{skill_id}/versions/{version_id}/smoke-test")
def smoke(skill_id: int, version_id: str, request: Request,
          payload: SandboxRequest,
          _: User = Depends(require_admin), db: Session = Depends(get_db)):
    result = skill_versions.sandbox_smoke_test(db, skill_id, version_id,
                                               payload.script, payload.sample_input)
    request.state.audit_changes = {"skill_test": result["status"]}
    return result


@router.post("/{skill_id}/versions/{version_id}/publish")
def publish(skill_id: int, version_id: str, payload: ReleaseRequest, request: Request,
            actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    publication = skill_versions.publish(db, skill_id, version_id, payload.revision, actor.id)
    request.state.audit_changes = {"skill_version": "published"}
    return {"version_id": publication.version_id, "revision": publication.revision}


@router.post("/{skill_id}/versions/{version_id}/rollback")
def rollback(skill_id: int, version_id: str, payload: ReleaseRequest, request: Request,
             actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    publication = skill_versions.publish(db, skill_id, version_id, payload.revision,
                                         actor.id, rollback=True)
    request.state.audit_changes = {"skill_version": "rolled_back"}
    return {"version_id": publication.version_id, "revision": publication.revision}

