"""技能包上传作业：请求内只做校验与暂存，重活交给后台任务，状态落库供前端轮询。

为什么要异步：落盘 + 全量同步 + 中文名生成串在请求里会把单 worker 的事件循环冻住十几秒
（历史故障：nginx 记 499、前端报"上传失败"、后端其实已经建好包），用户以为失败就重复上传、
删包重传，反而制造出僵尸包记录。

约定（与其它模块的耦合面只有 SkillService 与 skill_upload_jobs 表）：
- 暂存目录在技能卷内（skills/.staging-<job_id>/），与目标同一文件系统，收尾用 os.rename
  原子落地；点开头目录被所有扫描跳过，半成品既不会被同步注册也不会被 Agent 装载。
- 作业状态落库，刷新页面/换设备也能看到结果；进程重启后残留的非终态作业统一标记 failed
  （作业不跨重启续跑），并清理遗留暂存目录。
"""
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_upload_job import (
    JOB_DONE, JOB_EXTRACTING, JOB_FAILED, JOB_PENDING, JOB_SYNCING,
    TERMINAL_STATUSES, SkillUploadJob,
)

# 单次上传上限：与 nginx client_max_body_size 200m 对齐
UPLOAD_MAX_TOTAL_BYTES = 200 * 1024 * 1024
UPLOAD_MAX_FILE_COUNT = 2000
# 压缩工具/操作系统产生的垃圾文件，落盘时跳过
JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
# 暂存目录前缀（点开头：所有技能扫描都会跳过）
STAGING_PREFIX = ".staging-"
# 作业超过这个时长仍未完结即判定中断（后台任务可能因进程异常/响应未发出而从未执行）
STALE_JOB_MINUTES = 30


class SkillUploadError(ValueError):
    """上传校验或落盘失败；message 直接面向用户，API 层按 status_code 转 HTTPException。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ==================== 表与作业查询 ====================

def ensure_tables() -> None:
    """自建 skill_upload_jobs 表（create_all 幂等）：模块自包含，不改 database.py 的建表流程。"""
    from app.database import Base, engine
    Base.metadata.create_all(bind=engine, tables=[SkillUploadJob.__table__])


def create_job(db: Session, *, package_name: str, kind: str, file_count: int,
               total_bytes: int, created_by: Optional[int]) -> SkillUploadJob:
    job = SkillUploadJob(
        package_name=package_name, kind=kind, status=JOB_PENDING,
        message="已受理，等待后台处理", file_count=file_count,
        total_bytes=total_bytes, created_by=created_by,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: int) -> Optional[SkillUploadJob]:
    return db.query(SkillUploadJob).filter(SkillUploadJob.id == job_id).first()


def list_jobs(db: Session, limit: int = 20) -> List[SkillUploadJob]:
    return (db.query(SkillUploadJob)
            .order_by(SkillUploadJob.created_at.desc(), SkillUploadJob.id.desc())
            .limit(limit).all())


def has_running_job(db: Session) -> bool:
    """是否有未完结作业：用于拒绝并发上传（两次并发同步会互相覆盖）。"""
    return db.query(SkillUploadJob).filter(
        SkillUploadJob.status.notin_(TERMINAL_STATUSES)).count() > 0


def reap_stale_jobs(db: Session, max_age_minutes: int = STALE_JOB_MINUTES) -> int:
    """把超期未完结的作业标记失败。

    后台任务万一没跑起来（进程被换掉、响应没发出），作业行会永远停在 pending，
    之后每次上传都被 409 挡住；这里按创建时间兜底回收，不依赖重启。
    """
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
    stale = db.query(SkillUploadJob).filter(
        SkillUploadJob.status.notin_(TERMINAL_STATUSES),
        SkillUploadJob.created_at < cutoff,
    ).all()
    for job in stale:
        job.status = JOB_FAILED
        job.error = f"作业超过 {max_age_minutes} 分钟未完结，判定为中断"
        job.message = "已超时中断，请重新上传"
        job.finished_at = datetime.now()
    if stale:
        db.commit()
        print(f"[SkillUpload] 回收超期未完结作业 {len(stale)} 个")
    return len(stale)


def job_to_dict(job: SkillUploadJob) -> dict:
    def _loads(raw):
        try:
            return json.loads(raw) if raw else None
        except Exception:
            return None

    return {
        "id": job.id,
        "package_name": job.package_name,
        "kind": job.kind,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "file_count": job.file_count,
        "total_bytes": job.total_bytes,
        "new_skills": _loads(job.new_skills_json) or [],
        "stats": _loads(job.stats_json),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def mark_interrupted_jobs() -> int:
    """服务启动收尾：把上一次进程遗留的非终态作业标记 failed，并清理遗留暂存目录。"""
    from app.database import SessionLocal
    ensure_tables()
    db = SessionLocal()
    count = 0
    try:
        stale = db.query(SkillUploadJob).filter(
            SkillUploadJob.status.notin_(TERMINAL_STATUSES)).all()
        for job in stale:
            job.status = JOB_FAILED
            job.error = "服务重启导致作业中断，请重新上传"
            job.message = "已中断（服务重启）"
            job.finished_at = datetime.now()
            count += 1
        db.commit()
    finally:
        db.close()
    removed = cleanup_stale_staging()
    if count or removed:
        print(f"[SkillUpload] 启动收尾：标记中断作业 {count} 个，清理遗留暂存目录 {removed} 个")
    return count


# ==================== 路径与暂存 ====================

def skills_root() -> Path:
    from app.services.skill_service import SkillService
    return Path(SkillService._skills_root())


def staging_root(job_id: int) -> Path:
    return skills_root() / f"{STAGING_PREFIX}{job_id}"


def cleanup_stale_staging() -> int:
    """删除技能根目录下遗留的 .staging-* 目录（启动时调用：此时不可能有作业在跑）。"""
    root = skills_root()
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith(STAGING_PREFIX):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def is_junk_path(rel: str) -> bool:
    return any(part in JUNK_NAMES for part in rel.split("/"))


def validate_rel_path(rel: str) -> str:
    """规范化包内相对路径；绝对路径与路径穿越直接拒绝（zip-slip 防护）。"""
    norm = (rel or "").replace("\\", "/").strip()
    if not norm or norm.startswith("/") or ".." in norm.split("/"):
        raise SkillUploadError(f"非法文件路径: {rel}")
    return norm


def spool_to(src_fileobj, dest: Path) -> int:
    """把已 spool 的上传文件对象写到目标路径（纯同步 I/O，调用方放线程池），返回字节数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(dest, "wb") as out:
        src_fileobj.seek(0)
        while True:
            chunk = src_fileobj.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > UPLOAD_MAX_TOTAL_BYTES:
                raise SkillUploadError(
                    f"单个文件超过 {UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)}MB 上限", 413)
            out.write(chunk)
    return written


# ==================== zip 计划与解压 ====================

def plan_zip(zip_path: str, zip_filename: str) -> dict:
    """读 zip 中央目录做校验并给出落盘计划（不落盘）：包名、成员、相对路径。

    包名规则：zip 内只有单一顶层目录 → 用该目录名；否则用 zip 文件名（去扩展名）。
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = []
        for m in zf.namelist():
            if m.endswith("/"):
                continue
            if is_junk_path(validate_rel_path(m)):
                continue
            members.append(m)
        if not members:
            raise SkillUploadError("压缩包内没有可用文件")
        if len(members) > UPLOAD_MAX_FILE_COUNT:
            raise SkillUploadError(f"文件数量过多（上限 {UPLOAD_MAX_FILE_COUNT}）", 413)
        tops = {m.split("/", 1)[0] for m in members}
        strip_root = len(tops) == 1 and all("/" in m for m in members)
        package_name = tops.pop() if strip_root else Path(zip_filename or "package.zip").stem
        rels = [m.split("/", 1)[1] if strip_root else m for m in members]
        if not any(r.split("/")[-1] == "SKILL.md" for r in rels):
            raise SkillUploadError("不是有效的技能包：必须包含 SKILL.md")
        return {"package_name": package_name, "members": members, "rels": rels,
                "file_count": len(members)}


def extract_zip(zip_path: str, members: List[str], rels: List[str], dest: Path) -> int:
    """按已校验的计划解压到 dest（同步 I/O，调用方放线程池或后台任务），返回总字节数。"""
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member, rel in zip(members, rels):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > UPLOAD_MAX_TOTAL_BYTES:
                        raise SkillUploadError(
                            f"解压后大小超出限制（{UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)}MB）", 413)
                    out.write(chunk)
    return written


# ==================== 后台作业主体 ====================

def run_job(job_id: int) -> None:
    """后台执行：解压/落地 → 同步注册 → 排队中文名生成 → 落库作业结果。

    以同步函数形式交给 FastAPI BackgroundTasks（Starlette 会放线程池执行），
    全程不碰事件循环；任何异常都收敛成 job.status=failed，前端轮询即可看到原因。
    """
    from app.database import SessionLocal
    from app.services.skill_service import (
        SkillService, skill_mutation_lock, spawn_skill_enrichment,
    )

    db = SessionLocal()
    staging = staging_root(job_id)
    target: Optional[Path] = None
    try:
        job = get_job(db, job_id)
        if job is None:
            print(f"[SkillUpload] 作业 {job_id} 不存在，跳过")
            return
        if job.status in TERMINAL_STATUSES:
            return
        job.status = JOB_EXTRACTING
        job.message = "正在解压落盘"
        job.started_at = datetime.now()
        db.commit()

        package_name = job.package_name
        root = skills_root()
        root.mkdir(parents=True, exist_ok=True)
        target = root / package_name
        staged_pkg = staging / package_name

        before_ids = {skill.id for skill in db.query(Skill).all()}

        # 与同步/刷新/删除互斥：并发同步会互相覆盖。后台作业允许等锁（最多 60s），
        # 否则管理员顺手点一次刷新就会让正在处理的上传整个失败。
        with skill_mutation_lock(timeout=60):
            # zip：解压进暂存；文件夹：请求阶段已按最终布局写进暂存
            zip_path = staging / "package.zip"
            if zip_path.is_file():
                plan = plan_zip(str(zip_path), f"{package_name}.zip")
                if staged_pkg.exists():
                    shutil.rmtree(staged_pkg, ignore_errors=True)
                total = extract_zip(str(zip_path), plan["members"], plan["rels"], staged_pkg)
                job.total_bytes = total
                job.file_count = plan["file_count"]
                db.commit()
            if not staged_pkg.is_dir():
                raise SkillUploadError("暂存目录缺失，无法落地（可能已被清理），请重新上传")
            if target.exists():
                raise SkillUploadError(f"已存在同名技能包: {package_name}，请先删除或改名")
            # 原子落地：同一文件系统内改名，避免同步/Agent 扫描到半成品
            os.rename(str(staged_pkg), str(target))
            job.status = JOB_SYNCING
            job.message = "正在同步注册技能"
            db.commit()
            stats = SkillService(db).sync_from_filesystem()
            registered = [skill for skill in db.query(Skill).all() if skill.id not in before_ids]
            target = None
            job.new_skills_json = json.dumps([skill.name for skill in registered], ensure_ascii=False)
            db.commit()
            if job.created_by:
                try:
                    pkg = db.query(SkillPackage).filter(SkillPackage.name == job.package_name).first()
                    if pkg and not getattr(pkg, "created_by", None):
                        pkg.created_by = job.created_by
                        db.query(Skill).filter(Skill.package_id == pkg.id).update({"created_by": job.created_by})
                    # Also backfill standalone skills (no package_id) matching the upload name
                    db.query(Skill).filter(
                        Skill.name == job.package_name,
                        Skill.package_id.is_(None),
                        Skill.created_by.is_(None),
                    ).update({"created_by": job.created_by})
                    db.commit()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"set created_by failed: {e}")
            from app.services.skill_upload_publication import publish_uploaded_skills
            job.message = '正在隔离校验并发布新技能'
            db.commit()
            stats['published'] = publish_uploaded_skills(db, [skill.id for skill in registered], job.created_by)
            from app.core.cache import cache_delete_pattern
            cache_delete_pattern('skills:*')
            # 新技能要对 Agent 立即可见：清实例缓存（懒加载，避免与 agents 模块形成导入环）
            try:
                from app.agents.manager import agent_manager
                agent_manager.clear_cache()
            except Exception as e:
                print(f"[SkillUpload] Agent 缓存刷新失败（不影响上传结果）: {e}")

        new_skills = sorted(skill.name for skill in registered)
        job.status = JOB_DONE
        job.new_skills_json = json.dumps(new_skills, ensure_ascii=False)
        job.stats_json = json.dumps(
            {k: v for k, v in stats.items() if k != "pending_meta_package_ids"},
            ensure_ascii=False, default=str)
        job.message = (f"技能包 '{package_name}' 上传成功，新注册 {len(new_skills)} 个技能"
                       + (f"：{', '.join(new_skills)}" if new_skills else "")
                       + "（中文展示信息后台生成中）")
        job.finished_at = datetime.now()
        db.commit()
        print(f"[SkillUpload] 作业 {job_id} 完成: {job.message}")
        # 到这里包已落地且已入库：后续任何异常都不该再把目录回收掉
        target = None
        # 中文名/包摘要交给后台富化线程（模型调用不在作业线程里串行等待）
        spawn_skill_enrichment(package_ids=stats.get("pending_meta_package_ids") or [],
                               translate=True)
    except Exception as e:
        _fail_job(db, job_id, e)
        # 失败回收：只删本次作业刚落地/暂存的目录，绝不动已存在的同名包
        try:
            if target is not None and target.exists():
                shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        db.close()


def _fail_job(db: Session, job_id: int, err: Exception) -> None:
    """把异常收敛成作业失败状态（含面向用户的 message 与完整 error）。"""
    status_code = getattr(err, "status_code", None)
    message = getattr(err, "message", None) or str(err)
    try:
        # 先回滚：失败可能来自 DB 层，会话此时处于待回滚状态
        db.rollback()
        job = get_job(db, job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        job.status = JOB_FAILED
        job.error = f"{type(err).__name__}: {err}"[:4000]
        job.message = message[:500]
        job.finished_at = datetime.now()
        db.commit()
        print(f"[SkillUpload] 作业 {job_id} 失败"
              + (f"（HTTP {status_code}）" if status_code else "") + f": {message}")
    except Exception as e:
        print(f"[SkillUpload] 作业 {job_id} 失败状态落库异常: {e}")
