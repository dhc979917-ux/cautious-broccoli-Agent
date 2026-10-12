from app.services.skill_hub.legacy_guard import require_system_resource
from app.services.skill_hub import native_policy
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List
import asyncio
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from app.database import get_db
from app.services.skill_service import (
    SkillService, SkillBusyError, SkillDeleteConflictError, SkillDirMissingError,
    SkillScopeError, change_scope_in_new_session, spawn_skill_enrichment, sync_in_new_session,
)
# 上传作业：请求内只校验+暂存，落盘/同步/中文名生成在后台推进，状态落库可轮询
from app.services import skill_upload_service as upload_svc
from app.schemas.skill import (
    SkillResponse, CreateSkillRequest, UpdateSkillRequest, ScopeChangeRequest,
    SkillPackageResponse, UpdatePackageRequest,
)
from app.core.deps import get_current_user, require_admin, require_perm, require_module_perm
from app.core.security import verify_password
from app.models.skill import Skill
from app.models.user import User as UserModel
from app.schemas.destroy import DestroyRequest
from app.agents.manager import agent_manager
from app.models.user import User
from fastapi.encoders import jsonable_encoder
from app.core.cache import cache_delete_pattern, cache_get, cache_set

# 技能列表缓存（Redis cache-aside，REDIS_URL 未配置时自动 no-op）
SKILL_BASE_CACHE_KEY = "skills:base"
SKILL_BASE_CACHE_TTL = 30

# 上传上限由 skill_upload_service 统一定义，这里 re-export 兼容旧引用
UPLOAD_MAX_TOTAL_BYTES = upload_svc.UPLOAD_MAX_TOTAL_BYTES
UPLOAD_MAX_FILE_COUNT = upload_svc.UPLOAD_MAX_FILE_COUNT


def _sync_skills_in_thread() -> dict:
    """线程池里执行同步：转调 service 层的独立会话实现（保留旧名字，兼容既有调用方）。"""
    return sync_in_new_session()


def _busy(e: SkillBusyError) -> HTTPException:
    """技能变更锁被占用 → 409（前端提示稍后重试，不要重复提交）。"""
    return HTTPException(status_code=409, detail=str(e))


''' 技能列表查询'''

router = APIRouter(prefix="/skills", tags=["技能管理"], dependencies=[Depends(require_system_resource)])

@router.get("/")
async def get_all_skills_with_access(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取所有Skill列表，并标记当前用户是否有权限使用"""
    skill_service = SkillService(db)
    # 基础数据走 Redis cache-aside；has_access 依赖当前用户角色，每请求计算不入缓存
    base = cache_get(SKILL_BASE_CACHE_KEY)
    if base is None:
        from app.services.skill_hub.access import system_only
        all_skills = system_only(db.query(Skill), db, Skill).filter(Skill.is_active.is_(True)).all()
        packages = {p.id: p for p in skill_service.get_all_packages_include_deleted()}
        from app.models.user import User as _User
        _creator_ids = set()
        for _s in all_skills:
            if _s.created_by:
                _creator_ids.add(_s.created_by)
        for _p in packages.values():
            if getattr(_p, "created_by", None):
                _creator_ids.add(_p.created_by)
        _users = db.query(_User.id, _User.username).filter(_User.id.in_(_creator_ids)).all() if _creator_ids else []
        _user_map = {uid: uname for uid, uname in _users}
        from app.models.user import User as _User
        _creator_ids = set()
        for _s in all_skills:
            if _s.created_by:
                _creator_ids.add(_s.created_by)
        for _p in packages.values():
            if getattr(_p, "created_by", None):
                _creator_ids.add(_p.created_by)
        _users = db.query(_User.id, _User.username).filter(_User.id.in_(_creator_ids)).all() if _creator_ids else []
        _user_map = {uid: uname for uid, uname in _users}
        base = []
        for skill in all_skills:
            skill_data = {
                "id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "display_name_zh": skill.display_name_zh,
                "description_zh": skill.description_zh,
                "package_id": skill.package_id,
                "required_role": skill.required_role,
                "is_active": skill.is_active,
                "rel_path": skill.rel_path,
                "dir_missing": bool(skill.dir_missing),
                "is_hidden": bool(skill.is_hidden),
                "created_at": skill.created_at,
            }
            pkg = packages.get(skill.package_id) if skill.package_id else None
            if pkg is not None:
                skill_data["package_name"] = pkg.name
                skill_data["package_display_name"] = pkg.display_name
                skill_data["package_description"] = pkg.description
                skill_data["package_is_hidden"] = bool(pkg.is_hidden)
                if getattr(pkg, "created_by", None):
                    skill_data["package_uploaded_by"] = _user_map.get(pkg.created_by)
            if getattr(skill, "created_by", None):
                skill_data["uploaded_by"] = _user_map.get(skill.created_by)
            base.append(skill_data)
        cache_set(SKILL_BASE_CACHE_KEY, jsonable_encoder(base), ttl=SKILL_BASE_CACHE_TTL)

    # 决策 B 统一过滤：隐藏技能仅 admin/被授权用户可见；has_access 含授权覆盖
    return skill_service.filter_visible_skills_for_user(base, current_user)

@router.get("/all", response_model=List[SkillResponse])
async def get_all_skills_admin(
    current_user: User = Depends(require_module_perm("skill_manage")),
    db: Session = Depends(get_db)
):
    """获取所有Skill（包括已删除的，仅管理员）"""
    skill_service = SkillService(db)
    mapping = native_policy.policies(db)
    return [native_policy.describe(s, SkillResponse, mapping)
            for s in skill_service.get_all_skills_include_deleted()]

# ==================== 技能包管理 ====================

@router.get("/packages/all", response_model=List[SkillPackageResponse])
async def get_all_packages_admin(
    current_user: User = Depends(require_module_perm("skill_manage")),
    db: Session = Depends(get_db)
):
    """获取所有技能包（包括已软删除的，仅管理员）"""
    mapping = native_policy.policies(db)
    return [native_policy.describe(p, SkillPackageResponse, mapping, "package")
            for p in SkillService(db).get_all_packages_include_deleted()]

@router.put("/packages/{package_id}", response_model=SkillPackageResponse)
async def update_package(
    package_id: int,
    data: UpdatePackageRequest,
    current_user: User = Depends(require_perm("skill_manage.package.update")),
    db: Session = Depends(get_db)
):
    """编辑技能包（短名/总结，仅管理员）"""
    service = SkillService(db)
    native_policy.prepare(db, current_user, service.get_package_by_id(package_id), "package", data.allow_fork, data.model_dump())
    pkg = service.update_package(
        package_id,
        display_name=data.display_name,
        description=data.description,
        required_role=data.required_role,
        is_hidden=data.is_hidden,
    )
    cache_delete_pattern("skills:*")
    return native_policy.describe(pkg, SkillPackageResponse, native_policy.policies(db), "package")

@router.put('/packages/{package_id}/scope', response_model=SkillPackageResponse)
async def change_package_scope(package_id: int, data: ScopeChangeRequest,
                               current_user: User = Depends(require_perm('skill_manage.package.update')),
                               db: Session = Depends(get_db)):
    from app.services.skill_package_scope import change_package_scope as apply_scope
    try:
        package = apply_scope(db, package_id, data.scope)
    except SkillBusyError as error:
        raise _busy(error)
    agent_manager.clear_cache()
    cache_delete_pattern('skills:*')
    return package


@router.delete("/packages/{package_id}")
async def delete_package(
    package_id: int,
    current_user: User = Depends(require_perm("skill_manage.package.soft")),
    db: Session = Depends(get_db)
):
    """软删除技能包：成员技能保留，用户端整体隐藏（仅管理员）"""
    try:
        SkillService(db).delete_package(package_id)
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    agent_manager.clear_cache()
    cache_delete_pattern("skills:*")
    return {"message": "技能包已删除"}

@router.put("/packages/{package_id}/restore")
async def restore_package(
    package_id: int,
    current_user: User = Depends(require_perm("skill_manage.package.restore")),
    db: Session = Depends(get_db)
):
    """恢复技能包（仅管理员）"""
    try:
        SkillService(db).restore_package(package_id)
    except SkillDirMissingError as e:
        # 目录不在时不允许翻牌：否则下一次同步又把它打回软删除，管理端会看到"恢复了又自动被删"
        raise HTTPException(status_code=409, detail=str(e))
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    agent_manager.clear_cache()
    cache_delete_pattern("skills:*")
    return {"message": "技能包已恢复"}

@router.delete("/packages/{package_id}/hard")
async def hard_delete_package(
    package_id: int,
    data: DestroyRequest,
    current_user: User = Depends(require_perm("skill_manage.package.hard")),
    db: Session = Depends(get_db)
):
    """彻底删除技能包：包记录 + 包内所有技能（DB 与磁盘目录）一起删除（仅管理员）"""
    # 凭据按账号查找，兼容旧用户名
    admin_user = db.query(UserModel).filter(
        (UserModel.account == data.username) | (UserModel.username == data.username)
    ).first()
    if not admin_user or admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号不是管理员")
    if not verify_password(data.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="密码错误")
    try:
        result = SkillService(db).hard_delete_package(package_id)
        agent_manager.clear_cache()
        cache_delete_pattern("skills:*")
        if result.get("db_only"):
            return {"message": f"技能包 '{result['name']}' 的磁盘目录已不存在，已清理数据库记录"
                               f"（含 {len(result.get('members') or [])} 个成员技能）"}
        return {"message": f"技能包 '{result['name']}' 及其 "
                           f"{len(result.get('members') or [])} 个技能已彻底删除，数据不可恢复"}
    except SkillDeleteConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/", response_model=SkillResponse)
async def create_skill(
    data: CreateSkillRequest,
    current_user: User = Depends(require_perm("skill_manage.create")),
    db: Session = Depends(get_db)
):
    """创建新Skill（仅管理员）"""
    skill_service = SkillService(db)
    skill = skill_service.register_skill(
        name=data.name,
        display_name=data.display_name,
        description=data.description,
        required_role=data.required_role,
        is_hidden=data.is_hidden,
    )
    cache_delete_pattern("skills:*")
    return skill

@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: int,
    data: UpdateSkillRequest,
    current_user: User = Depends(require_perm("skill_manage.update")),
    db: Session = Depends(get_db)
):
    """更新Skill（仅管理员）"""
    skill_service = SkillService(db)
    try:
        native_policy.prepare(db, current_user, skill_service.get_skill_by_id(skill_id), "skill", data.allow_fork, data.model_dump())
        skill = skill_service.update_skill(
            skill_id=skill_id,
            display_name=data.display_name,
            description=data.description,
            required_role=data.required_role,
            is_hidden=data.is_hidden,
        )
        cache_delete_pattern("skills:*")
        return native_policy.describe(skill, SkillResponse, native_policy.policies(db))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{skill_id}/scope", response_model=SkillResponse)
async def change_skill_scope(
    skill_id: int,
    data: ScopeChangeRequest,
    current_user: User = Depends(require_perm("skill_manage.update")),
    db: Session = Depends(get_db)
):
    """切换技能归属：可选 / 工具助手内置 / 数据分析师内置（剪切语义，目录随之移动）。

    移动目录 + 全量重扫是磁盘密集操作，放线程池并用独立数据库会话执行（口径同 /refresh），
    避免把单 worker 的事件循环堵住。完成后清 Agent 缓存：已构建的 Agent 仍持有旧的
    技能目录快照，不清就要等下次重建才生效。
    """
    try:
        await asyncio.to_thread(change_scope_in_new_session, skill_id, data.scope)
    except SkillBusyError as e:
        raise _busy(e)
    except SkillScopeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    agent_manager.clear_cache()
    cache_delete_pattern("skills:*")
    # 线程里用的是另一个会话：过期后重读，才能拿到移动后的 rel_path 与内置标志
    db.expire_all()
    try:
        return SkillService(db).get_skill_by_id(skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: int,
    current_user: User = Depends(require_perm("skill_manage.delete.soft")),
    db: Session = Depends(get_db)
):
    """删除Skill（软删除，仅管理员）"""
    skill_service = SkillService(db)
    try:
        skill_service.delete_skill(skill_id)
        agent_manager.clear_cache()
        cache_delete_pattern("skills:*")
        return {"message": "Skill已删除"}
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.put("/{skill_id}/restore")
async def restore_skill(
    skill_id: int,
    current_user: User = Depends(require_perm("skill_manage.delete.restore")),
    db: Session = Depends(get_db)
):
    """恢复Skill（仅管理员）"""
    skill_service = SkillService(db)
    try:
        skill_service.restore_skill(skill_id)
        agent_manager.clear_cache()
        cache_delete_pattern("skills:*")
        return {"message": "Skill已恢复"}
    except SkillDirMissingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/{skill_id}/hard")
async def hard_delete_skill(
    skill_id: int,
    data: DestroyRequest,
    current_user: User = Depends(require_perm("skill_manage.delete.hard")),
    db: Session = Depends(get_db)
):
    """彻底删除Skill（硬删除，需要重新输入管理员密码）"""
    # 验证管理员密码
    # 凭据按账号查找，兼容旧用户名
    admin_user = db.query(UserModel).filter(
        (UserModel.account == data.username) | (UserModel.username == data.username)
    ).first()
    if not admin_user or admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号不是管理员")
    if not verify_password(data.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="密码错误")
    
    skill_service = SkillService(db)
    try:
        result = skill_service.hard_delete_skill(skill_id)
        cache_delete_pattern("skills:*")
        if result.get("db_only"):
            return {"message": f"Skill '{result['name']}' 的磁盘目录已不存在，已清理数据库记录"}
        return {"message": f"Skill '{result['name']}' 已彻底删除（含磁盘目录），数据不可恢复"}
    except SkillDeleteConflictError as e:
        # 目录里还嵌着别的技能（技能目录同时是包根）/ 同名目录有多个候选 → 拒绝，不猜着删
        raise HTTPException(status_code=409, detail=str(e))
    except SkillBusyError as e:
        raise _busy(e)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/refresh")
async def refresh_skill_cache(
    current_user: User = Depends(require_perm("skill_manage.refresh")),
    db: Session = Depends(get_db)
):
    """同步文件系统技能到数据库并刷新Agent缓存（管理员）

    技能包丢进 skills/ 目录（支持嵌套）后调用本接口即可自动注册，
    无需逐个手动注册。

    同步是纯本地操作（不读 README、不调模型），仍放线程池执行并使用独立数据库会话：
    这里曾遗留一次"在事件循环里直接同步"的调用，遇到包摘要的模型调用会把单 worker
    冻住约 20 秒（nginx 记 499，前端以为容器挂了）。
    """
    try:
        stats = await asyncio.to_thread(_sync_skills_in_thread)
    except SkillBusyError as e:
        raise _busy(e)
    agent_manager.clear_cache()
    cache_delete_pattern("skills:*")
    # 包摘要与技能中文名生成交给后台富化线程（模型调用不进请求路径）
    spawn_skill_enrichment(package_ids=stats.get("pending_meta_package_ids") or [], translate=True)
    if stats.get("aborted"):
        raise HTTPException(
            status_code=503,
            detail=f"技能同步已中止（{stats['aborted']}）：可选技能目录不可用，本次未停用任何记录；"
                   "请检查技能卷挂载后重试")
    return {"message": (f"同步完成：新增 {stats['created']} 个，更新 {stats['updated']} 个，"
                        f"停用 {stats['deleted']} 个，恢复 {stats['reactivated']} 个，"
                        f"耗时 {stats['elapsed_ms']}ms；Agent 缓存已刷新（中文展示信息后台生成中）")}


# ============ 管理员上传技能包（异步作业） ============
#
# 请求内只做"校验 + 暂存"，立即返回 202 + job_id；解压落地、同步注册、中文名生成
# 全部在后台任务里推进（见 app/services/skill_upload_service.py）。前端轮询作业状态，
# 因此不会再出现"前端 30s 超时报失败、后端其实已经建好包"的错位。


def _reject_existing_name(package_name: str) -> None:
    """重名检查：三个技能根目录下都不能已有同名目录（否则落地会覆盖既有技能）。"""
    for root_dir, kind in SkillService._skills_roots():
        if (Path(root_dir) / package_name).exists():
            loc = "可选" if kind is None else "内置"
            raise HTTPException(
                status_code=400,
                detail=f"已有重名技能 '{package_name}'（位于{loc}目录），请改名后重新上传")


@router.post("/upload-package", status_code=202)
async def upload_skill_package(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    paths: List[str] = Form(default=[]),
    current_user: User = Depends(require_perm("skill_manage.package.upload")),
    db: Session = Depends(get_db)
):
    """管理员上传技能包：文件夹（files + 相对路径）或单个 zip。

    返回 202 + job_id；进度与结果轮询 GET /skills/upload-jobs/{job_id}。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    # 先回收超期未完结的作业，避免"后台任务没跑起来 → 之后每次上传都被 409 挡住"
    upload_svc.reap_stale_jobs(db)
    if upload_svc.has_running_job(db):
        raise HTTPException(status_code=409, detail="已有上传作业正在进行，请等它完成后再上传")
    if len(files) > UPLOAD_MAX_FILE_COUNT:
        raise HTTPException(status_code=413, detail=f"文件数量过多（上限 {UPLOAD_MAX_FILE_COUNT}）")

    is_zip = len(files) == 1 and (files[0].filename or "").lower().endswith(".zip")
    root = upload_svc.skills_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"技能目录不可写，无法上传: {e}")

    # 暂存目录先用临时名，作业建好后再改名为 .staging-<job_id>（同一文件系统内，原子）。
    # 点开头目录被所有技能扫描跳过：半成品不会被同步注册，也不会被 Agent 装载。
    staging = root / f"{upload_svc.STAGING_PREFIX}tmp-{uuid.uuid4().hex[:12]}"
    staging.mkdir(parents=True, exist_ok=True)
    limit_mb = UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024)
    try:
        if is_zip:
            # ---------- 单个 zip：先暂存原始包，解压交给后台作业 ----------
            zip_path = staging / "package.zip"
            size = await asyncio.to_thread(upload_svc.spool_to, files[0].file, zip_path)
            if size > UPLOAD_MAX_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail=f"上传大小超出限制（{limit_mb}MB）")
            try:
                plan = await asyncio.to_thread(upload_svc.plan_zip, str(zip_path), files[0].filename or "")
            except upload_svc.SkillUploadError as e:
                raise HTTPException(status_code=e.status_code, detail=e.message)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="文件不是有效的 ZIP 技能包，请重新选择")
            package_name, kind = plan["package_name"], "zip"
            file_count, total_bytes = plan["file_count"], size
        else:
            # ---------- 文件夹拖拽：按最终布局直接写进暂存目录 ----------
            if len(paths) != len(files):
                raise HTTPException(status_code=400, detail="文件与路径数量不一致")
            try:
                rels = [upload_svc.validate_rel_path(p) for p in paths]
            except upload_svc.SkillUploadError as e:
                raise HTTPException(status_code=e.status_code, detail=e.message)
            tops = {r.split("/", 1)[0] for r in rels}
            if len(tops) != 1:
                raise HTTPException(status_code=400, detail="只支持单个顶层文件夹")
            if any("/" not in r for r in rels):
                raise HTTPException(status_code=400, detail="文件必须位于顶层文件夹内")
            package_name = tops.pop()
            kept = [(f, r) for f, r in zip(files, rels) if not upload_svc.is_junk_path(r)]
            if not any(r.split("/")[-1] == "SKILL.md" for _, r in kept):
                raise HTTPException(status_code=400, detail="不是有效的技能包：必须包含 SKILL.md")
            total_bytes = 0
            for uf, rel in kept:
                # rel 已含顶层目录前缀（上面校验过单一顶层），落盘时剥掉，避免多嵌套一层
                dest = staging / package_name / rel.split("/", 1)[1]
                total_bytes += await asyncio.to_thread(upload_svc.spool_to, uf.file, dest)
                if total_bytes > UPLOAD_MAX_TOTAL_BYTES:
                    raise HTTPException(status_code=413, detail=f"上传大小超出限制（{limit_mb}MB）")
            kind, file_count = "folder", len(kept)

        _reject_existing_name(package_name)
        job = upload_svc.create_job(
            db, package_name=package_name, kind=kind, file_count=file_count,
            total_bytes=total_bytes, created_by=getattr(current_user, "id", None))
        os.replace(str(staging), str(upload_svc.staging_root(job.id)))
        staging = None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传暂存失败: {e}")
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    background_tasks.add_task(upload_svc.run_job, job.id)
    return {
        "job_id": job.id,
        "package_name": job.package_name,
        "status": job.status,
        "message": f"已受理技能包 '{job.package_name}'（{job.file_count} 个文件），后台处理中",
    }


@router.get("/upload-jobs")
async def list_upload_jobs(
    limit: int = 20,
    current_user: User = Depends(require_module_perm("skill_manage")),
    db: Session = Depends(get_db)
):
    """最近的上传作业（管理端上传历史；也用于判断是否有作业在跑，跑着就禁用刷新按钮）"""
    return [upload_svc.job_to_dict(j) for j in upload_svc.list_jobs(db, max(1, min(limit, 100)))]


@router.get("/upload-jobs/{job_id}")
async def get_upload_job(
    job_id: int,
    current_user: User = Depends(require_module_perm("skill_manage")),
    db: Session = Depends(get_db)
):
    """轮询单个上传作业的进度与结果"""
    job = upload_svc.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="上传作业不存在")
    return upload_svc.job_to_dict(job)

