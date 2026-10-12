"""沙箱容器泄漏回收器（docker 模式兜底）。

每任务的沙箱回收由 AgentManager._evict_agent 的后台任务负责，但进程重启、
异常终止等场景仍可能残留 as_ws_* 容器。本模块定期强制回收兜底：

- 启动时：回收所有在本进程启动前创建的沙箱容器（进程重启后内存缓存清零，
  旧容器已无人持有，全部视为孤儿）；
- 运行期间：每小时清扫一次，回收存活超过 WORKSPACE_SANDBOX_MAX_AGE_HOURS、
  且没有运行中任务持有的容器。
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 与 agentscope DockerWorkspace 的容器命名保持一致（as_ws_{workspace_id}）
CONTAINER_NAME_PREFIX = "as_ws_"


async def _force_remove(container, name: str) -> bool:
    """强杀并删除单个容器（已停止的容器 kill 失败可忽略）。"""
    try:
        await container.kill()
    except (Exception, asyncio.CancelledError):
        pass
    try:
        await container.delete(force=True)
        return True
    except (Exception, asyncio.CancelledError) as e:
        logger.warning(f"[sandbox-reaper] 回收 {name} 失败: {e}")
        return False


async def _expire_previews(container_name: str) -> None:
    """沙箱被回收后，它发布出去的服务预览必须同步置为失效。

    沙箱是预览的唯一后端，容器没了 URL 就只剩代理回的说明页；库里不置失效，
    前端会继续显示一个"运行中"的死链。失败只记 debug：预览是增强项，
    不能反过来影响容器回收。
    """
    try:
        from app.services import preview_service
        if preview_service.preview_enabled():
            await preview_service.expire_by_container(container_name)
    except (Exception, asyncio.CancelledError) as e:
        logger.debug(f"[sandbox-reaper] 预览失效处理跳过: {e}")


async def sweep_stale_sandboxes(
    max_age_seconds: float = 0,
    created_before: Optional[float] = None,
) -> int:
    """强制回收泄漏的 as_ws_* 容器，返回回收数量。

    筛选模式：
    - created_before 非 None：回收在该时间戳之前创建的容器（启动清孤儿用）；
    - 否则：回收存活超过 max_age_seconds 的容器（周期清扫用）。

    归属处理：容器对应的 Agent 若仍在缓存中——
    - 会话任务运行中：跳过，避免误杀在用沙箱；
    - 空闲：先逐出 Agent（后台释放），再回收容器，保持一致性。
    """
    try:
        import aiodocker
    except ImportError:
        return 0

    from app.agents.manager import agent_manager

    removed = 0
    client = None
    try:
        client = aiodocker.Docker()
        now = time.time()
        containers = await client.containers.list(all=True)
        for c in containers:
            # containers.list 返回 DockerContainer 对象，原始 inspect 数据在 _container
            info = getattr(c, "_container", None) or {}
            names = info.get("Names") or []
            name = (names[0] if names else info.get("Name") or "").lstrip("/")
            if not name.startswith(CONTAINER_NAME_PREFIX):
                continue
            created = info.get("Created") or 0
            if created_before is not None:
                if created > created_before:
                    continue
            elif now - created < max_age_seconds:
                continue
            ws_id = name[len(CONTAINER_NAME_PREFIX):]
            cache_key = agent_manager.find_cache_key_by_workspace_id(ws_id)
            if cache_key is not None:
                if agent_manager._session_running(cache_key):
                    continue  # 任务运行中，留给下轮清扫
                await agent_manager._evict_agent(cache_key)
            if await _force_remove(c, name):
                removed += 1
                age = int(now - created)
                logger.info(f"[sandbox-reaper] 已回收泄漏沙箱容器 {name}（存活 {age}s）")
                await _expire_previews(name)
    except Exception as e:
        logger.debug(f"[sandbox-reaper] 本轮清扫跳过（docker 不可用？）: {e}")
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return removed


async def reaper_loop(interval_seconds: int = 3600, max_age_hours: float = 2) -> None:
    """常驻任务：启动清孤儿 + 定期清扫泄漏容器。"""
    startup_ts = time.time()
    try:
        n = await sweep_stale_sandboxes(created_before=startup_ts)
        if n:
            logger.info(f"[sandbox-reaper] 启动清理：回收 {n} 个孤儿沙箱容器")
    except Exception as e:
        logger.warning(f"[sandbox-reaper] 启动清理失败: {e}")

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            n = await sweep_stale_sandboxes(max_age_seconds=max_age_hours * 3600)
            if n:
                logger.info(f"[sandbox-reaper] 定期清扫：回收 {n} 个泄漏沙箱容器")
        except Exception as e:
            logger.warning(f"[sandbox-reaper] 定期清扫失败: {e}")
