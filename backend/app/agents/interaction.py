"""官方外部执行（HITL）的交互桥。

AskUserChoice 是 is_external_tool：模型调用后 AgentScope 发出
RequireExternalExecutionEvent 暂停回复，manager.chat_stream 的恢复循环经此桥
把问题推给前端并等待用户选择；用户选择（或超时/跳过/中断）后组装成
ExternalExecutionResultEvent 或 UserInterruptEvent 再次调用 reply_stream 恢复。

生命周期：
- chat.py 后台任务开始时创建 InteractionChannel 并注册到 registry（session_id -> channel），
  同时 set 到 contextvar（manager 侧通过 current_interaction 读取）；
- 任务结束（正常/异常）时注销并 cancel_all，防止悬挂的 future；
- WebSocket 侧通过 get_channel(session_id) 找到通道，把用户的选择/跳过写回
  （resolve/cancel），或在暂停态停止任务（interrupt_all）。

决策问题与结果记录在 records，任务落库时同步进 steps（刷新后可还原）。
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# 超时兜底文案：作为工具结果返回给模型，模型据此自选最优方案继续执行
TIMEOUT_TEXT = (
    "用户已超过 10 分钟未响应。请选择你认为最优的方案继续执行，"
    "并在最终回复中简要说明用户未响应、你选择了哪个方案及理由。"
)
# 用户主动跳过时的文案
CANCEL_TEXT = (
    "用户跳过了本次选择。请选择你认为最优的方案继续执行，"
    "并在最终回复中简要说明选择理由。"
)
# 无交互通道（如 REST 非流式路径）时的兜底文案
NO_CHANNEL_TEXT = (
    "当前没有可用的交互通道（用户不在线）。请选择你认为最优的方案继续执行，"
    "并在最终回复中简要说明选择理由。"
)

# 需求采集表单（CollectRequirements）专用兜底文案：语义是"按你填的 assume 继续"，
# 不是"从若干方案里挑一个"，因此不能复用上面三条。
FORM_TIMEOUT_TEXT = (
    "用户未在规定时间内填写需求表单。请直接采用你为每题填写的 assume 值继续实现，"
    "不要重复提问；把这些值全部记为「假设，待确认」，写进产物的规格说明，"
    "并在最终回复末尾列出待确认清单。"
)
FORM_CANCEL_TEXT = (
    "用户跳过了需求表单。请直接采用你为每题填写的 assume 值继续实现，"
    "不要重复提问；把这些值全部记为「假设，待确认」，写进产物的规格说明，"
    "并在最终回复末尾列出待确认清单。"
)
FORM_NO_CHANNEL_TEXT = (
    "当前没有可用的交互通道（用户不在线），无法采集需求。"
    "请直接采用你为每题填写的 assume 值继续实现，把它们全部记为「假设，待确认」，"
    "写进产物的规格说明，并在最终回复末尾列出待确认清单。"
)

# records[*]["kind"] 的取值：choice=AskUserChoice 决策卡，intake=需求采集表单
KIND_CHOICE = "choice"
KIND_INTAKE = "intake"

# 中断哨兵：用户在暂停态停止任务时，ask_external 返回该值，
# 恢复循环据此用 UserInterruptEvent 干净收尾被暂停的回复
INTERRUPT = object()


@dataclass
class InteractionChannel:
    """单个后台任务的交互通道：持有外部执行的待决 future 注册表。"""

    session_id: int
    emit: Callable[[dict], Awaitable[None]]
    user_id: Optional[int] = None
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    # request_id（即官方 tool_call_id）-> 交互记录，任务落库时随 steps 同步
    # （刷新后可还原决策卡 / 需求表单）。公共键：
    #   request_id / reply_id / question / options / answer / resolved_by / kind
    # kind=intake 时额外带 questions（表单题目定义）
    records: Dict[str, dict] = field(default_factory=dict)
    # 早到答案缓冲：决策卡先推给前端、future 在 reply_stream 收尾后才注册，
    # 用户在窗口内的点击先存这里（rid -> (answer, resolved_by)），ask_external 注册时兑现
    early: Dict[str, tuple] = field(default_factory=dict)
    announced: Dict[str, str] = field(default_factory=dict)

    def announce(self, request_id: str, kind: str = KIND_CHOICE) -> None:
        self.announced[str(request_id)] = kind

    async def ask_external(
        self,
        request_id: str,
        reply_id: str,
        question: str,
        options: list,
        timeout: Optional[float] = None,
    ):
        """登记一个待决外部执行（以 tool_call_id 为键）并阻塞等待用户答复。

        返回：用户选择文本 / TIMEOUT_TEXT / CANCEL_TEXT，或 INTERRUPT 哨兵（用户停止）。
        require_external_execution 事件由调用方（chat_stream）发出，这里只登记
        future 并等待；产生结果时 emit external_resolved 供前端关闭选项卡。
        """
        return await self._register_and_wait(
            request_id=request_id,
            reply_id=reply_id,
            record={
                "kind": KIND_CHOICE,
                "question": question,
                "options": options,
            },
            timeout=timeout,
            timeout_text=TIMEOUT_TEXT,
        )

    async def ask_confirmation(self, request_id: str, reply_id: str, tool_name: str):
        from app.core.presence import is_online

        return await self._register_and_wait(
            request_id=request_id,
            reply_id=reply_id,
            record={
                "kind": "confirm",
                "question": f"是否允许执行工具 {tool_name}？",
                "options": [
                    {"key": "A", "label": "允许执行"},
                    {"key": "B", "label": "拒绝执行"},
                ],
            },
            timeout=min(settings.ASK_USER_TIMEOUT_SECONDS, 60) if is_online(self.user_id) else 0,
            timeout_text="拒绝执行",
        )

    async def ask_external_form(
        self,
        request_id: str,
        reply_id: str,
        context: str,
        questions: list,
        timeout: Optional[float] = None,
    ):
        """登记一个待决的需求采集表单并阻塞等待用户提交。

        与 ask_external 共用 pending / records / early / resolve / cancel 全套机制，
        差别只在记录里多带 questions、兜底文案换成表单专用版本。
        返回：用户提交的 JSON 字符串 / FORM_TIMEOUT_TEXT / FORM_CANCEL_TEXT，
        或 INTERRUPT 哨兵。
        """
        return await self._register_and_wait(
            request_id=request_id,
            reply_id=reply_id,
            record={
                "kind": KIND_INTAKE,
                "question": context,
                "options": [],
                "questions": questions,
            },
            timeout=timeout,
            timeout_text=FORM_TIMEOUT_TEXT,
        )

    async def _register_and_wait(
        self,
        request_id: str,
        reply_id: str,
        record: dict,
        timeout: Optional[float],
        timeout_text: str,
    ):
        """登记待决 future + 兑现早到答案 + 等待/超时（两类外部执行共用）。"""
        request_id = str(request_id)
        timeout = timeout if timeout is not None else settings.ASK_USER_TIMEOUT_SECONDS
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[request_id] = fut
        self.announced.pop(request_id, None)
        self.records[request_id] = {
            "request_id": request_id,
            "reply_id": reply_id,
            "answer": None,
            "resolved_by": None,
            **record,
        }
        # 点击早于注册（卡片先推给了前端）：直接兑现早到答案，不再等待
        _early = self.early.pop(request_id, None)
        if _early is not None:
            _answer, _resolved_by = _early
            rec = self.records[request_id]
            rec["answer"] = _answer if _resolved_by == "user" else None
            rec["resolved_by"] = _resolved_by
            self.pending.pop(request_id, None)
            await self.emit({
                "type": "external_resolved",
                "request_id": request_id,
                "answer": rec["answer"],
                "resolved_by": _resolved_by,
            })
            return _answer
        try:
            answer = await asyncio.wait_for(fut, timeout=timeout)
            return answer
        except asyncio.TimeoutError:
            rec = self.records.get(request_id)
            if rec:
                rec["resolved_by"] = "timeout"
            await self.emit({
                "type": "external_resolved",
                "request_id": request_id,
                "answer": None,
                "resolved_by": "timeout",
            })
            return timeout_text
        finally:
            self.pending.pop(request_id, None)

    def _cancel_text(self, request_id: str) -> str:
        """按交互类型挑跳过文案：表单跳过要说"按 assume 继续"，不是"挑个方案"。"""
        rec = self.records.get(str(request_id)) or {}
        kind = rec.get("kind") or self.announced.get(str(request_id))
        if kind == "confirm":
            return "拒绝执行"
        return FORM_CANCEL_TEXT if kind == KIND_INTAKE else CANCEL_TEXT

    async def resolve(self, request_id: str, answer: str) -> bool:
        """用户选择了某方案（或填了自定义内容）。"""
        request_id = str(request_id)
        fut = self.pending.get(request_id)
        if fut is not None and fut.done():
            logger.warning(
                "[interaction] resolve dup: rid=%r 已处理", request_id,
            )
            return False
        if fut is None:
            # 已注册过但不在 pending = 已完成（ask_external 结束后 finally 弹出）：
            # 按重复点击处理；从未注册过才是竞态窗口的早到
            if request_id in self.records:
                logger.warning("[interaction] resolve dup: rid=%r 已完成", request_id)
                return False
            if request_id not in self.announced:
                return False
            # future 尚未注册：决策卡已推给前端但 reply_stream 还没收尾，
            # 用户点击早到——先缓冲，ask_external 注册时兑现（不再报 miss）
            self.early[request_id] = (answer, "user")
            rec = self.records.get(request_id)
            if rec:
                rec["answer"] = answer
                rec["resolved_by"] = "user"
            logger.info("[interaction] resolve 早到缓冲: rid=%r", request_id)
            await self.emit({
                "type": "external_resolved",
                "request_id": request_id,
                "answer": answer,
                "resolved_by": "user",
            })
            return True
        rec = self.records.get(request_id)
        if rec:
            rec["answer"] = answer
            rec["resolved_by"] = "user"
        fut.set_result(answer)
        await self.emit({
            "type": "external_resolved",
            "request_id": request_id,
            "answer": answer,
            "resolved_by": "user",
        })
        return True

    async def cancel(self, request_id: str) -> bool:
        """用户跳过选择。"""
        request_id = str(request_id)
        fut = self.pending.get(request_id)
        if fut is not None and fut.done():
            logger.warning("[interaction] cancel dup: rid=%r 已处理", request_id)
            return False
        if fut is None:
            if request_id in self.records:
                logger.warning("[interaction] cancel dup: rid=%r 已完成", request_id)
                return False
            if request_id not in self.announced:
                return False
            # 与 resolve 相同的竞态窗口：早到的跳过先缓冲，注册时兑现为 CANCEL_TEXT
            self.early[request_id] = (self._cancel_text(request_id), "cancel")
            rec = self.records.get(request_id)
            if rec:
                rec["resolved_by"] = "cancel"
            logger.info("[interaction] cancel 早到缓冲: rid=%r", request_id)
            await self.emit({
                "type": "external_resolved",
                "request_id": request_id,
                "answer": None,
                "resolved_by": "cancel",
            })
            return True
        rec = self.records.get(request_id)
        if rec:
            rec["resolved_by"] = "cancel"
        fut.set_result(self._cancel_text(request_id))
        await self.emit({
            "type": "external_resolved",
            "request_id": request_id,
            "answer": None,
            "resolved_by": "cancel",
        })
        return True

    def has_pending(self) -> bool:
        """是否有等待用户答复的外部执行（暂停态判定）。"""
        return any(not fut.done() for fut in self.pending.values())

    async def interrupt_all(self) -> None:
        """用户在暂停态停止任务：所有待决决策以 INTERRUPT 哨兵结束，
        恢复循环改用 UserInterruptEvent 干净收尾被暂停的回复。
        同时给前端推 external_resolved，决策卡实时关闭（停止后不残留）。"""
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                rec = self.records.get(rid)
                if rec:
                    rec["resolved_by"] = "interrupt"
                fut.set_result(INTERRUPT)
                try:
                    await self.emit({
                        "type": "external_resolved",
                        "request_id": rid,
                        "answer": None,
                        "resolved_by": "interrupt",
                    })
                except (Exception, asyncio.CancelledError):
                    pass

    async def cancel_all(self) -> None:
        """任务终止时兜底：所有待决决策以跳过文案结束，避免悬挂 future，
        并给前端推 external_resolved，防止决策卡在任务死后残留。"""
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                rec = self.records.get(rid)
                if rec and not rec.get("resolved_by"):
                    rec["resolved_by"] = "cancel"
                fut.set_result(self._cancel_text(rid))
                try:
                    await self.emit({
                        "type": "external_resolved",
                        "request_id": rid,
                        "answer": None,
                        "resolved_by": "cancel",
                    })
                except (Exception, asyncio.CancelledError):
                    pass
        self.pending.clear()


# manager 侧入口：chat_stream 恢复循环从这里拿当前任务的通道（None = 无交互通道，如 REST 路径）
current_interaction: ContextVar[Optional[InteractionChannel]] = ContextVar(
    "current_interaction", default=None
)

# WS 侧入口：按会话找通道
_registry: Dict[int, InteractionChannel] = {}


def register_channel(channel: InteractionChannel) -> None:
    _registry[channel.session_id] = channel


def unregister_channel(session_id: int) -> None:
    _registry.pop(session_id, None)


def get_channel(session_id: int) -> Optional[InteractionChannel]:
    return _registry.get(session_id)
