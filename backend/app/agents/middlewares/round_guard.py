"""RoundGuardMiddleware：软轮次上限——超过 N 轮注入一次收尾提醒（不硬停）。

为什么需要（2026-09-22 大纲 P2-3）：
dialog 架构默认不设 max_react_iters（=10 万），实际停止条件只剩 token 预算
（100 万 ≈ 无刹车）。失控会话（模型反复试探不收敛）会一轮一轮地把全量上下文
反复重发。硬停归 max_react_iters / token 预算管，本中间件只负责"提醒"：
到达软上限时向上下文注入一次收尾提示，之后的轮次照常执行（模型仍有自主权，
只是被明确告知该收了）。每回复最多提醒一次，不刷存在感。

与 ProactiveBudgetMiddleware 的分工：那个按 token 花钱提醒，这个按轮次提醒；
提示注入方式一致（HintBlock 附加到最后一条 assistant 消息）。
"""

import logging
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.message import AssistantMsg, HintBlock
from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)


class RoundGuardMiddleware(MiddlewareBase):
    """轮次软上限提醒。round_limit<=0 时完全不启用（零开销）。"""

    def __init__(self, round_limit: int = 0) -> None:
        self.round_limit = int(round_limit or 0)
        self._warned: set = set()   # 已提醒过的 reply_id（每回复只提醒一次）

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        if self.round_limit > 0:
            try:
                self._count_and_warn(agent)
            except Exception as e:  # 计数失败不阻断推理
                logger.debug("[round_guard] 处理失败（忽略）: %s", e)
        async for event in next_handler(**input_kwargs):
            yield event

    def _count_and_warn(self, agent: "Agent") -> None:
        state = agent.state
        reply_id = str(getattr(state, "reply_id", "") or "")
        store = state.middle_context.setdefault("round_guard", {})
        count = int(store.get(reply_id, 0)) + 1
        store[reply_id] = count
        # 防长会话 middle_context 膨胀：只留最近的 reply 记录
        while len(store) > 8:
            store.pop(next(iter(store)), None)
        if count == self.round_limit and reply_id not in self._warned:
            self._warned.add(reply_id)
            logger.warning(
                "[round_guard] session=%s reply=%s 已达软轮次上限 %d，注入收尾提醒",
                getattr(state, "session_id", "?"), reply_id, self.round_limit,
            )
            hint = HintBlock(
                hint=(
                    f"本轮回复已进行 {count} 轮推理-工具循环，超过常规上限"
                    f"（{self.round_limit} 轮）。请立即评估：核心目标是否已达成？"
                    "已达成则用现有成果收尾输出；未达成则只保留最关键的剩余步骤，"
                    "停止一切探索性/重复性工具调用。"
                )
            )
            ctx = state.context
            if ctx and ctx[-1].role == "assistant" and ctx[-1].name == agent.name:
                ctx[-1].content.append(hint)
            else:
                ctx.append(AssistantMsg(id=reply_id, name=agent.name, content=[hint]))
