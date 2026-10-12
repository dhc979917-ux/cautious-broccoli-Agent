"""会话后台任务管理器（Agent 执行与 WebSocket 连接解耦）

之前：Agent 执行绑定在 WebSocket 连接里，用户切页面/断连即中断任务，结果丢失。
现在：任务以独立 asyncio 任务在服务端运行，事件流缓存在内存；
     WebSocket 只做"订阅/转发"，断开不影响执行，重新进入会话可回放续看。

约束：每个会话同时只允许一个运行中任务（与前端发送锁一致）。
局限：服务器进程重启会丢失运行中任务（要跨重启需引入任务队列，另议）。
"""

import logging

logger = logging.getLogger(__name__)

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional


class SessionBusyError(RuntimeError):
    pass


@dataclass
class TaskInfo:
    session_id: int
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    last_token_usage: Optional[dict] = None
    events: list = field(default_factory=list)   # 客户端形态事件流（chunk/thinking/tool_call/done/file_output/end/error）
    status: str = "running"                       # running / done / error / stopped
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    cancel_requested: bool = False                # cancel() 置位；worker 据此把收尾落库标记为"用户停止"
    task: Optional[asyncio.Task] = None           # 运行协程引用（cancel 用）
    # 续播偏移：每次排队内容投递（queued_delivered）后更新为当前事件下标。
    # 投递前的内容已分段落库，重连回放只需从该点继续，避免重复渲染
    replay_from: int = 0
    # 启动时的模型与技能快照：stop 后自动续发新任务时，前端没传就沿用这些
    model: Optional[str] = None
    selected_skills: Optional[list] = None
    # 任务归属用户：管理端强制下线时按用户批量停任务（见 services/task_control.py）
    user_id: Optional[int] = None
    # 停止原因：由停止方写入（user_stopped / admin_revoked），worker 落库 metadata.stop_reason
    stop_reason: Optional[str] = None

    async def emit(self, event: dict):
        """任务侧追加一个事件并唤醒所有订阅者"""
        async with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    async def finish(self, status: str):
        async with self.cond:
            self.status = status
            self.finished_at = time.time()
            self.cond.notify_all()

    async def subscribe(self, from_index: int = 0) -> AsyncIterator[dict]:
        """回放 + 实时跟踪事件流；任务结束且事件取完后迭代结束。

        晚到的订阅者（断线重连）会把已缓冲事件一次性回放完再跟踪新事件。
        """
        idx = max(0, from_index)
        while True:
            async with self.cond:
                await self.cond.wait_for(
                    lambda: len(self.events) > idx or self.status != "running"
                )
                batch = self.events[idx:]
                idx = len(self.events)
                terminal = self.status != "running"
            for event in batch:
                yield event
            if terminal:
                break


class TaskManager:
    def __init__(self):
        self._infos: Dict[int, TaskInfo] = {}

    # 已完成任务的回放缓存最多保留的条数与秒数（超限时从最旧的开始清理），
    # 防止事件流缓冲在大量会话/长时间运行后无限增长
    MAX_FINISHED_KEPT = 200
    FINISHED_TTL_SECONDS = 300

    def _prune(self):
        """清理已完成任务的回放缓存（过期或超量），新任务启动前调用"""
        now = time.time()
        expired = [
            sid for sid, info in self._infos.items()
            if info.status != "running"
            and info.finished_at is not None
            and now - info.finished_at > self.FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            self._infos.pop(sid, None)
        finished = sorted(
            (sid for sid, info in self._infos.items() if info.status != "running"),
            key=lambda sid: self._infos[sid].finished_at or 0,
        )
        for sid in finished[: max(0, len(finished) - self.MAX_FINISHED_KEPT)]:
            self._infos.pop(sid, None)

    def get(self, session_id: int) -> Optional[TaskInfo]:
        return self._infos.get(session_id)

    def is_running(self, session_id: int) -> bool:
        info = self._infos.get(session_id)
        return bool(info and info.status == "running")

    def running_session_ids(self, user_id: Optional[int] = None) -> list:
        """正在运行的会话 id 列表；给了 user_id 就只返回该用户名下的任务。

        不查数据库：任务启动时已把归属用户记在 TaskInfo 上（chat.py 的
        _start_agent_task），这里只做一次内存快照遍历。
        """
        return [
            sid for sid, info in list(self._infos.items())
            if info.status == "running" and (user_id is None or info.user_id == int(user_id))
        ]

    def start(self, session_id: int, worker) -> TaskInfo:
        """启动后台任务。worker 是接收 emit 回调的协程工厂；
        同一会话的旧任务信息（含已完成任务的回放缓存）被新任务直接替换。"""
        if self.is_running(session_id):
            raise SessionBusyError(f"session {session_id} already has a running task")
        self._prune()
        info = TaskInfo(session_id=session_id)
        self._infos[session_id] = info
        info.task = asyncio.get_running_loop().create_task(self._run(info, worker))
        return info

    def reserve(self, session_id: int) -> TaskInfo:
        if self.is_running(session_id):
            raise SessionBusyError(f"session {session_id} already has a running task")
        self._prune()
        info = TaskInfo(session_id=session_id, task=asyncio.current_task())
        self._infos[session_id] = info
        return info

    async def cancel(self, session_id: int, timeout: float = 20.0) -> Optional[TaskInfo]:
        """请求停止会话的运行中任务并等待其到达终态。

        AgentScope 会吞掉 CancelledError 并优雅收尾（产出 INTERRUPTED 的 ReplyEnd），
        所以这里只负责置 cancel_requested 标记 + cancel 协程 + 等终态；
        落库与事件收尾由 worker 依据 cancel_requested 完成。超时不阻塞调用方。
        """
        info = self._infos.get(session_id)
        if info is None or info.status != "running":
            return info
        info.cancel_requested = True
        if info.task is not None:
            info.task.cancel()
        try:
            async with info.cond:
                await asyncio.wait_for(
                    info.cond.wait_for(lambda: info.status != "running"),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            logger.info(f"[TaskManager] 会话 {session_id} 停止等待超时，继续后续流程")
        # 兜底：任务在"第一次被事件循环调度前"就被取消时，协程体根本没开始执行，
        # _run 里的 CancelledError 收尾分支不会运行，status 会永远停在 running，
        # 该会话此后再也提交不了新任务（前端一直收到"有任务正在运行"）。这里补记终态。
        if info.status == "running" and info.task is not None and info.task.done():
            logger.info(f"[TaskManager] 会话 {session_id} 任务在启动前即被取消，补记终态 stopped")
            await info.finish("stopped")
        return info

    async def wait_finished(self, session_id: int, timeout: float = 20.0) -> Optional[TaskInfo]:
        """等待任务到达终态（不主动取消）。

        用于暂停态中断：哨兵已让恢复循环自行收束，只需等终态。超时不阻塞调用方。
        """
        info = self._infos.get(session_id)
        if info is None:
            return info
        try:
            async with info.cond:
                await asyncio.wait_for(
                    info.cond.wait_for(lambda: info.status != "running"),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            logger.info(f"[TaskManager] 会话 {session_id} 等待终态超时")
        return info

    async def _run(self, info: TaskInfo, worker):
        try:
            await worker(info.emit)
            # cancel_requested 时 worker 通常已优雅收尾（AgentScope 吞掉 CancelledError），
            # 状态如实记为 stopped，供前端/续发流程判断
            await info.finish("stopped" if info.cancel_requested else "done")
        except asyncio.CancelledError:
            # worker 没吞掉取消时走到这里：尽量记终态，避免 status 永远停在 running
            try:
                await info.finish("stopped")
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception(f"[后台任务] 会话 {info.session_id} 执行失败: {e}")
            try:
                await info.emit({"type": "error", "content": f"Agent处理失败: {e}"})
            except Exception:
                pass
            await info.finish("error")


# 全局单例：整个进程共享（后台任务注册表）
task_manager = TaskManager()
