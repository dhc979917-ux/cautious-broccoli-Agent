"""
后端程序入口，启动服务器

启动命令:
  python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import logging

logger = logging.getLogger(__name__)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request
from contextlib import asynccontextmanager
import threading
import asyncio
from datetime import datetime, timedelta
from app.config import settings, validate_runtime_settings
from app.core.logging import setup_logging
from app.database import init_db, SessionLocal
from sqlalchemy import text
# 技能同步/富化的入口都在 API 与 service 层，启动只做编排（不直接持有 SkillService）
from app.services.skill_service import spawn_skill_enrichment, sync_in_new_session
from app.api.v1 import auth, agents, sessions, users, chat, skills, upload, models, files
from app.api.v1 import knowledge, mcp, monitoring, admin_audit, admin_people, admin_models, admin_tools, admin_skills, admin_agents, admin_memories, admin_operations
from app.api.v1 import scheduled_jobs, notifications  # 调度任务模块（自包含）
from app.services import ocr_gateway
from app.api.v1 import skill_resources
from app.api.v1 import previews  # 服务预览（沙箱内服务的临时对外发布）
from app.core.tracing import setup_tracing

# 统一日志：在任何业务日志输出前完成 "app" logger 初始化（幂等）
setup_logging(level=settings.LOG_LEVEL, app_env=settings.APP_ENV)


def _start_health_probe_thread() -> None:
    """B4：独立线程健康探针（与业务事件循环解耦）。

    Docker healthcheck 原本打在业务事件循环上的 /health：一次长同步段
    （记忆提炼/上下文压缩等）饿死循环就会导致探针超时、容器被误判
    unhealthy。这里用单独 daemon 线程跑一个极小的 stdlib HTTP 服务，
    healthcheck 改打它——业务循环被卡时探针仍能如实应答"进程活着"。
    """
    import json as _json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.agents import activity

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/health":
                self.send_error(404)
                return
            body = _json.dumps({
                "status": "ok",
                "event_loop_lag": round(activity.last_lag(), 3),
                "active_turns": activity.snapshot(),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # 静默：探针每 20s 一次，不刷访问日志
            pass

    port = settings.HEALTH_PROBE_PORT
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as e:
        logger.warning(f"[WARN] 独立健康探针启动失败（端口 {port}）: {e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True, name="health-probe").start()
    logger.info(f"[OK] 独立线程健康探针已启动（:{port}/health，与业务事件循环解耦）")


# 生命周期钩子：服务器开始接受请求之前执行一次启动初始化（lifespan 是 on_event("startup") 的官方替代写法）
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_settings()
    """应用启动时执行"""
    logger.info("=" * 50)
    logger.info("  企业级 Agent 平台 - 后端服务启动中...")
    logger.info("=" * 50)
    
    # B4：独立线程健康探针（先于一切业务初始化，启动全程 Docker 探针可应答）
    _start_health_probe_thread()

    # 初始化数据库（根据 ORM 模型定义在数据库里建表）
    init_db()
    logger.info("[OK] 数据库初始化完成")
    from app.services.mcp_descriptions import enqueue_descriptions
    enqueue_descriptions()

    # B2：DB 连接池预热——执行一次简单查询，消除首次 API 请求的连接池初始化
    # 冷启动延迟（诊断报告实测 5.5 秒）。init_db() 只建表不预热连接池。
    try:
        _warm_db = SessionLocal()
        try:
            _warm_db.execute(text("SELECT 1"))
            _warm_db.commit()
        finally:
            _warm_db.close()
        logger.info("[OK] 数据库连接池预热完成")
    except Exception as e:
        logger.warning(f"[WARN] 数据库连接池预热失败（不影响启动）: {e}")

    # 知识库启动自愈：重启会打断后台解析任务，把卡在
    # converting/parsing/indexing 的文档复位为 failed，允许用户重试
    from app.services.skill_resource_changes import recover_changes
    await asyncio.to_thread(recover_changes)
    from app.services.knowledge_service import recover_stale_documents
    recover_stale_documents()
    
    # 初始化 OpenTelemetry 追踪（TracingMiddleware 依赖此初始化）
    if settings.TRACING_ENABLED:
        setup_tracing()
        logger.info("[OK] 追踪(Tracing)已启用")
    
    # 同步文件系统技能到数据库（技能包丢进 skills/ 后启动即自动注册）
    db = SessionLocal()
    try:
        # 上传作业表自建 + 上一次进程遗留的非终态作业标记失败（前端不会永久转圈）
        try:
            from app.services.skill_upload_service import mark_interrupted_jobs
            mark_interrupted_jobs()
        except Exception as e:
            logger.warning(f"[WARN] 技能上传作业收尾失败（不影响启动）: {e}")

        # 同步是纯本地操作（不读 README、不调模型），仍放线程池执行，避免大目录拖慢启动；
        # service 层封装了独立数据库会话，不把启动流程的会话跨线程传递
        stats = await asyncio.to_thread(sync_in_new_session)
        logger.info(f"[OK] 技能同步完成: 新增 {stats['created']} 个, 更新 {stats['updated']} 个, "
                    f"停用 {stats['deleted']} 个, 恢复 {stats['reactivated']} 个, "
                    f"停用包 {stats['packages_deactivated']} 个, 耗时 {stats['elapsed_ms']}ms")
        if stats.get("aborted"):
            # 可选技能卷没挂上/播种失败：本次没有停用任何记录，但必须让运维看见
            logger.error(f"[ERROR] 技能同步中止（{stats['aborted']}）：可选技能目录不可用，"
                         "已跳过全部停用动作；请检查 skills 卷挂载与 entrypoint 播种结果")
        for name in stats.get("deactivated_skills", []):
            logger.warning(f"[WARN] 停用技能（磁盘上找不到目录）: {name}")
        for name in stats.get("deactivated_packages", []):
            logger.warning(f"[WARN] 停用技能包（磁盘上找不到目录）: {name}")
        for line in stats.get("conflicts", []):
            logger.warning(f"[WARN] 技能目录冲突: {line}")
        # 包摘要与技能中文展示名：交给后台富化线程串行生成（模型调用不阻塞 startup）
        spawn_skill_enrichment(package_ids=stats.get("pending_meta_package_ids") or [], translate=True)
    finally:
        db.close()
    
    # 记忆生命周期调度器：每日凌晨 03:00 吸收昨日记忆卡为当日总结并清理原文，
    # 周满聚合周总结、月满聚合月总结（逻辑在 app/agents/memory.py）。
    # 周/月聚合与日提炼都是补偿式的：漏跑一次由下一次运行补齐，不会永久丢。
    # 另外启动 10 分钟后补跑一次，覆盖"容器恰好在 03:00 前后重启"导致的漏跑。
    async def _memory_scheduler():
        from app.agents.memory import run_janitor_once
        if settings.MEMORY_JANITOR_STARTUP_CATCHUP:
            await asyncio.sleep(settings.MEMORY_JANITOR_STARTUP_DELAY_SECONDS)
            try:
                stats = await run_janitor_once()
                logger.info(f"[memory-janitor] 启动补跑完成: {stats}")
            except Exception as e:
                logger.error(f"[memory-janitor] 启动补跑失败: {e}")
        while True:
            now = datetime.now()
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                stats = await run_janitor_once()
                logger.info(f"[memory-janitor] 完成: {stats}")
            except Exception as e:
                logger.error(f"[memory-janitor] 运行失败: {e}")

    scheduler_task = asyncio.create_task(_memory_scheduler())
    from app.services.memory_governance import run_worker as memory_governance_worker
    memory_worker_task = asyncio.create_task(memory_governance_worker())
    logger.info("[OK] 记忆生命周期调度器已启动（每日凌晨 03:00，24小时制）")

    # Agent 实例每日清理：一会话一沙箱常驻保留上下文，每天凌晨 04:00 统一回收
    # 空闲实例（含 MCP 连接/沙箱）；删除会话时由 API 侧调用 evict_session 即时清理
    async def _agent_cache_scheduler():
        from app.agents.manager import agent_manager
        while True:
            now = datetime.now()
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await agent_manager.evict_idle_all()
            except Exception as e:
                logger.error(f"[agent-cache] 每日清理失败: {e}")

    agent_cache_task = asyncio.create_task(_agent_cache_scheduler())
    logger.info("[OK] Agent 实例每日清理调度器已启动（每日凌晨 04:00）")

    # 调度任务模块：启动调度引擎并从数据库重建任务注册（自包含，失败不阻塞启动）
    scheduled_jobs_started = False
    if settings.SCHEDULER_ENABLED:
        from app.services.scheduler_service import scheduler_service
        try:
            await scheduler_service.start()
            scheduled_jobs_started = True
        except Exception as e:
            logger.error(f"[WARN] 调度任务引擎启动失败（不影响启动）: {e}")

    # 沙箱容器回收器：启动清孤儿 + 定期清扫泄漏的 as_ws_* 容器（仅 docker 沙箱模式）
    reaper_task = None
    loop_lag_task = None
    preview_task = None
    if getattr(settings, "WORKSPACE_BACKEND", "local") == "docker":
        from app.agents.sandbox_reaper import reaper_loop
        reaper_task = asyncio.create_task(reaper_loop(
            max_age_hours=getattr(settings, "WORKSPACE_SANDBOX_MAX_AGE_HOURS", 2),
        ))
        logger.info("[OK] 沙箱容器回收器已启动（启动清孤儿 + 每小时清扫泄漏容器）")

        # 服务预览（PREVIEW_ENABLED 总开关）：启动清孤儿代理容器 + 周期回收到期预览。
        # 进程重启后内存态清零，上一轮留下的 tp_preview_* 容器没人认领，既占宿主端口
        # 又没人访问，必须在这里对账清掉；库里的 active 记录同步置为失效。
        from app.services import preview_service
        if preview_service.preview_enabled():
            if not preview_service.public_base_url():
                logger.warning(
                    "[WARN] PREVIEW_ENABLED=true 但未配置 PREVIEW_PUBLIC_BASE_URL，"
                    "预览地址将拼不出来（前端只能显示端口）"
                )
            try:
                cleaned = await preview_service.sweep_orphan_sidecars()
                if cleaned:
                    logger.info(f"[OK] 服务预览：启动清理 {cleaned} 个无主代理容器/记录")
            except Exception as e:
                logger.warning(f"[WARN] 服务预览启动清理失败（不影响启动）: {e}")
            preview_task = asyncio.create_task(preview_service.preview_reaper_loop())
            logger.info("[OK] 服务预览回收器已启动（每 5 分钟回收到期预览并释放宿主端口）")

        # 事件循环阻塞监控：AgentScope ReAct 循环里的同步段（工具表组装/技能装载）
        # 会饿死 uvicorn 单线程循环，导致 /health 探针超时。这里周期性测量循环 tick
        # 的实际间隔，超过阈值即告警；D7：告警附带活跃回合快照（session_id+阶段/工具名），
        # 直接定位"哪个会话的哪一步把循环卡住了"。
        async def _event_loop_lag_monitor():
            import time as _time
            from app.agents import activity
            threshold = 3.0
            interval = 5.0
            while True:
                t0 = _time.monotonic()
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    break
                lag = _time.monotonic() - t0 - interval
                activity.record_lag(lag)
                if lag > threshold:
                    logger.warning(
                        f"[eventloop] lag={lag:.2f}s (>{threshold}s) "
                        "事件循环被阻塞/饱和，可能拖垮 /health 探针 "
                        f"active=[{'; '.join(activity.snapshot())}]"
                    )

        loop_lag_task = asyncio.create_task(_event_loop_lag_monitor())
        logger.info("[OK] 事件循环阻塞监控已启动（阈值 3s）")

    from app.services.knowledge.index_jobs import enqueue_repairs, run_worker
    repair_counts = await asyncio.to_thread(enqueue_repairs)
    index_worker_task = asyncio.create_task(run_worker())
    logger.info("[knowledge-jobs] recovery queued: %s", repair_counts)
    logger.info(f"[OK] 服务运行在: http://{settings.APP_HOST}:{settings.APP_PORT}")
    logger.info(f"[OK] API文档: http://localhost:{settings.APP_PORT}/docs")
    logger.info("=" * 50)
    from app.services.skill_evolution import worker as evolution_worker
    evolution_worker.start()
    yield
    await evolution_worker.stop()

    # 关停收尾：停调度器、关闭内嵌 ReMe 应用
    scheduler_task.cancel()
    memory_worker_task.cancel()
    await asyncio.gather(memory_worker_task, return_exceptions=True)
    index_worker_task.cancel()
    await asyncio.gather(index_worker_task, return_exceptions=True)
    agent_cache_task.cancel()
    if scheduled_jobs_started:
        try:
            from app.services.scheduler_service import scheduler_service as _sched_service
            await _sched_service.shutdown()
        except Exception:
            pass
    if loop_lag_task is not None:
        loop_lag_task.cancel()
    if reaper_task is not None:
        reaper_task.cancel()
    if preview_task is not None:
        preview_task.cancel()
    try:
        from app.agents.memory import close_all
        await close_all()
    except Exception:
        pass

# 创建FastAPI应用实例（app 是整个后端的核心对象，所有路由、中间件都挂在它上面）
app = FastAPI(
    title="企业级 Agent 平台",
    description="基于 AgentScope 2.0 的企业级AI Agent管理平台",
    version="1.0.0",
    lifespan=lifespan
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    if request.url.path.startswith("/api/v1/mcp/"):
        return JSONResponse({"detail": "Invalid MCP request"}, status_code=422)
    return await request_validation_exception_handler(request, error)

# CORS 中间件配置（允许前端跨域访问）
# 浏览器有同源策略，前端跑在 localhost:5173（Vite 默认端口），后端跑在 localhost:8000，端口不同算跨域。不加这个中间件，前端的 fetch/axios 请求会被浏览器拦截。
#  四个参数的含义：
#   - allow_origins：白名单，只允许这两个地址跨域访问
#   - allow_credentials=True：允许跨域请求携带 Cookie（JWT 场景下其实不需要，但留着无害）
#   - allow_methods=["*"]：允许所有 HTTP 方法（GET、POST、PUT、DELETE 等）
#   - allow_headers=["*"]：允许所有请求头（包括 Authorization: Bearer xxx）
from app.middleware.activity_tracker import ActivityTrackerMiddleware
app.add_middleware(ActivityTrackerMiddleware)
from app.middleware.admin_audit import AdminAuditMiddleware
app.add_middleware(AdminAuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],  # 白名单由 CORS_ORIGINS 配置
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册API路由
app.include_router(auth.router, prefix="/api/v1") # 最终路径是 /api/v1/auth/login
app.include_router(agents.router, prefix="/api/v1")
app.include_router(sessions.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(skills.router, prefix="/api/v1")
app.include_router(upload.router, prefix="/api/v1")
app.include_router(models.router, prefix="/api/v1")
app.include_router(ocr_gateway.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(knowledge.router, prefix="/api/v1")
app.include_router(knowledge.progress_ws_router, prefix="/api/v1")
app.include_router(mcp.router, prefix="/api/v1")
app.include_router(admin_audit.router, prefix="/api/v1")
app.include_router(admin_people.router, prefix="/api/v1")
app.include_router(admin_models.router, prefix="/api/v1")
app.include_router(admin_tools.router, prefix="/api/v1")
app.include_router(admin_skills.router, prefix="/api/v1")
app.include_router(skill_resources.router, prefix="/api/v1")
app.include_router(admin_agents.router, prefix="/api/v1")
app.include_router(admin_memories.router, prefix="/api/v1")
app.include_router(admin_operations.router, prefix='/api/v1')
app.include_router(monitoring.router, prefix="/api/v1")
app.include_router(scheduled_jobs.router, prefix="/api/v1")   # 调度任务模块
app.include_router(notifications.router, prefix="/api/v1")    # 调度任务模块（站内通知）
app.include_router(previews.router, prefix="/api/v1")         # 服务预览

# 根路径，返回应用基本信息。部署后访问根路径能看到服务是否正常
@app.get("/")
async def root():
    """根路径健康检查"""
    return {
        "message": "企业级 Agent 平台 API",
        "version": "1.0.0",
        "docs": "/docs"
    }

# 标准健康检查端点，Docker/K8s 等容器编排工具会定期调这个接口判断服务是否存活，返回非 200 就自动重启
@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "ok"}

@app.get("/ready")
async def readiness_check():
    """就绪检查（readiness）：数据库可达即就绪；liveness 仍用 /health。"""
    from app.database import SessionLocal
    from sqlalchemy import text
    from fastapi import HTTPException
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="not ready") from None
    finally:
        db.close()

from app.api.v1 import skill_hub, admin_skill_hub
app.include_router(skill_hub.router, prefix="/api/v1")
app.include_router(admin_skill_hub.router, prefix="/api/v1")

from app.api.v1 import skill_evolution
app.include_router(skill_evolution.router, prefix="/api/v1")
