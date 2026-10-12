"""Shared administrator file browser and recoverable resource changes."""

from app.services.skill_hub.legacy_guard import require_system_resource
import asyncio
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import require_admin
from app.database import SessionLocal, get_db
from app.models.skill_governance import SkillReleaseEvent, SkillVersion
from app.models.skill_resource import PackageVersion, ResourceChangeJob
from app.models.user import User
from app.services import skill_resource_changes as changes
from app.services.skill_resource_files import TOTAL_LIMIT, download_type, preview, relative_path, safe_file
from app.services.skill_service import SkillBusyError, skill_mutation_lock


router = APIRouter(prefix='/admin/skill-resources', tags=['Skill资源文件'], dependencies=[Depends(require_system_resource)])


@contextmanager
def resource_lock(timeout=0):
    try:
        with skill_mutation_lock(timeout=timeout):
            yield
    except SkillBusyError as exc:
        raise HTTPException(409, str(exc)) from exc


class FileEdit(BaseModel):
    path: str = Field(max_length=1000)
    content: str = Field(max_length=2 * 1024 * 1024)
    version_id: str = Field(max_length=36)
    revision: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def job_description(job):
    return {'id': job.id, 'resource_type': job.resource_type, 'resource_id': job.resource_id,
            'operation': job.operation, 'status': job.status, 'message': job.message,
            'files': job.plan.get('files', []), 'members': job.plan.get('members', {}),
            'result': job.result, 'created_at': job.created_at}


def _job(db, identifier, kind, resource_id):
    job = db.get(ResourceChangeJob, identifier)
    if job is None or job.resource_type != kind or job.resource_id != resource_id:
        raise HTTPException(404, '更新作业不存在')
    return job


def _version(db, kind, identifier, version_id):
    changes.resource(db, kind, identifier)
    model = PackageVersion if kind == 'package' else SkillVersion
    version = db.get(model, version_id)
    owner = getattr(version, 'package_id', None) if kind == 'package' else getattr(version, 'skill_id', None)
    if version is None or owner != identifier or (kind == 'skill' and version.status != 'published'):
        raise HTTPException(404, '资源历史版本不存在')
    return version


@router.get('/{kind}/{identifier}/history')
def history(kind: str, identifier: int, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    with resource_lock(timeout=30):
        entity, publication, current_version = changes.current(db, kind, identifier)
    model = PackageVersion if kind == 'package' else SkillVersion
    query = db.query(model).filter_by(**{'package_id' if kind == 'package' else 'skill_id': identifier})
    if kind == 'skill':
        query = query.filter_by(status='published')
    rows = query.order_by(model.created_at, model.id).all()
    labels = {}
    if kind == 'skill':
        events = db.query(SkillReleaseEvent).filter_by(skill_id=identifier).order_by(SkillReleaseEvent.id).all()
        ordered = list(dict.fromkeys(event.version_id for event in events))
        labels = {key: number for number, key in enumerate(ordered)}
    descriptions = []
    for index, row in enumerate(rows):
        number = row.number if kind == 'package' else labels.get(row.id, index)
        descriptions.append({'id': row.id, 'number': number, 'label': '原始版' if number == 0 else f'v{number}版',
                             'created_at': row.created_at, 'current': row.id == publication.version_id,
                             'files': len(row.manifest)})
    descriptions.sort(key=lambda item: item['number'])
    jobs = db.query(ResourceChangeJob).filter_by(resource_type=kind, resource_id=identifier).order_by(ResourceChangeJob.created_at.desc()).limit(10).all()
    return {'resource_type': kind, 'resource_id': identifier, 'name': entity.display_name,
            'version_id': publication.version_id, 'revision': publication.revision,
            'editable': bool(entity.is_active and not entity.dir_missing),
            'versions': descriptions,
            'jobs': [job_description(job) for job in jobs]}


@router.get('/{kind}/{identifier}/versions/{version_id}/entries')
def entries(kind: str, identifier: int, version_id: str, path: str = '',
            actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = _version(db, kind, identifier, version_id)
    relative_path(path, allow_root=True)
    changes.version_directory(version, kind)
    prefix = path.rstrip('/') + '/' if path else ''
    items = {}
    for entry in version.manifest:
        if not entry['path'].startswith(prefix):
            continue
        remainder = entry['path'][len(prefix):]
        name, separator, tail = remainder.partition('/')
        if not name:
            continue
        items[name] = {'name': name, 'path': prefix + name, 'kind': 'directory' if separator else 'file',
                       'bytes': None if separator else entry['bytes']}
    if path and not items:
        raise HTTPException(404, '目录不存在')
    return sorted(items.values(), key=lambda item: (item['kind'] != 'directory', item['name'].casefold()))


@router.get('/{kind}/{identifier}/versions/{version_id}/content')
def file_content(kind: str, identifier: int, version_id: str, path: str,
                 actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = _version(db, kind, identifier, version_id)
    return preview(changes.version_directory(version, kind), path)


@router.get('/{kind}/{identifier}/versions/{version_id}/download')
def download(kind: str, identifier: int, version_id: str, path: str, inline: bool = False,
             actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    version = _version(db, kind, identifier, version_id)
    file_path = safe_file(changes.version_directory(version, kind), path)
    return FileResponse(file_path, media_type=download_type(file_path, inline), filename=file_path.name,
                        content_disposition_type='inline' if inline else 'attachment',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
                                 'Content-Security-Policy': "default-src 'none'; sandbox"})


@router.post('/{kind}/{identifier}/edits', status_code=202)
async def edit(kind: str, identifier: int, body: FileEdit, tasks: BackgroundTasks, request: Request,
               actor: User = Depends(require_admin)):
    def prepare():
        with SessionLocal() as db:
            job = changes.prepare_edit(db, kind, identifier, actor.id, **body.model_dump())
            return job_description(job) if job else None
    try:
        job = await asyncio.to_thread(prepare)
    except SkillBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    request.state.audit_changes = {'path': body.path, 'before_version': body.version_id,
                                   'job_id': job['id'] if job else None}
    if job is None:
        return {'id': None, 'status': 'done', 'message': '内容未变化，无需创建新版本', 'result': {'unchanged': True}}
    tasks.add_task(changes.run_job, job['id'])
    return job


@router.post('/{kind}/{identifier}/uploads', status_code=202)
async def upload(kind: str, identifier: int, tasks: BackgroundTasks, file: UploadFile = File(...),
                 actor: User = Depends(require_admin)):
    if not (file.filename or '').lower().endswith('.zip'):
        raise HTTPException(422, '请上传ZIP更新包')
    base = Path(settings.KNOWLEDGE_DIR).resolve() / 'skill_resource_uploads'
    base.mkdir(parents=True, exist_ok=True)
    archive = base / (str(uuid4()) + '.zip')
    try:
        total = 0
        with archive.open('wb') as stream:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > TOTAL_LIMIT:
                    raise HTTPException(413, '更新包超过200MiB限制')
                stream.write(chunk)
        with SessionLocal() as db:
            with resource_lock():
                job, entity, version, source = changes._create_job(db, kind, identifier, actor.id, 'upload')
                job.plan = {**job.plan, 'archive_name': archive.name, 'filename': file.filename}
                job.status = 'planning'
                job.message = '已上传，正在分析文件及成员差异'
                db.commit()
                response = job_description(job)
        tasks.add_task(changes.run_upload_plan, response['id'])
        return response
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get('/{kind}/{identifier}/jobs/{job_id}')
def job_status(kind: str, identifier: int, job_id: str, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    return job_description(_job(db, job_id, kind, identifier))


@router.post('/{kind}/{identifier}/jobs/{job_id}/confirm')
def confirm(kind: str, identifier: int, job_id: str, tasks: BackgroundTasks,
            actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    with resource_lock():
        job = changes.confirm_job(db, _job(db, job_id, kind, identifier))
    if job.status == 'queued':
        tasks.add_task(changes.run_job, job.id)
    return job_description(job)


@router.post('/{kind}/{identifier}/jobs/{job_id}/cancel')
def cancel(kind: str, identifier: int, job_id: str, actor: User = Depends(require_admin), db: Session = Depends(get_db)):
    with resource_lock():
        job = _job(db, job_id, kind, identifier)
        if job.status != 'planned':
            raise HTTPException(409, '此更新已开始处理，无法取消')
        source, staging, backup = changes._job_paths(job)
        changes._cleanup_paths(source, staging, backup)
        job.status = 'cancelled'
        job.message = '已取消，服务器文件未修改'
        db.commit()
    return job_description(job)

