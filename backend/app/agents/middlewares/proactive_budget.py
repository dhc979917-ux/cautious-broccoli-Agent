"""ProactiveBudgetMiddleware: progressive budget warnings before hard cutoff.

Extends ReplyBudgetControlMiddleware to inject reminders at 70% and 90% of
the token budget, giving the model time to prioritise and wrap up gracefully
instead of being abruptly cut off at 100%.
"""

import logging
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import ReplyBudgetControlMiddleware
from agentscope.message import AssistantMsg, HintBlock

from app.agents import delivery_profile as dp

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# 警告文案里必须把"验证"从可跳过项里摘出来：以前写的是"跳过非必要的验证"，
# 等于在预算压力下第一个被牺牲的就是交付质量（本次改造的直接动因之一）。
# 真正该省的是重复读取与探索性搜索。
_WARN_70 = (
    "你已消耗约 70% 的 token 预算。请优先完成核心步骤，"
    "跳过重复读取和探索性搜索，确保关键交付物先行完成。"
    "注意：需求采集与交付前验证不属于可跳过项。"
)
_WARN_90 = (
    "预算即将耗尽（已用约 90%）。立即用当前已有成果收尾，"
    "不要再调用工具探索新方向，直接给出最终回复。"
    "若产物已生成但未经 VerifyArtifact 验证，必须在回复中显式写明「未验证」及原因。"
)


class ProactiveBudgetMiddleware(ReplyBudgetControlMiddleware):
    """Budget control with progressive warnings at 70% / 90% / 100%.

    At 70%: inject a HintBlock reminding the model to prioritise.
    At 90%: inject a stronger HintBlock to wrap up immediately.
    At 100%: delegate to parent (forces tool_choice=none).

    档位感知：构建型任务（artifact 档）要额外承担需求采集与交付前验证的固定
    开销，用基础预算会把它们挤掉。传入 ``artifact_token_budget`` 后，该档位
    自动改用更大的预算（取两者较大值），其余档位行为完全不变。
    """

    def __init__(self, *args, artifact_token_budget: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._base_budget = float(self.token_budget or 0)
        self._artifact_budget = float(artifact_token_budget or 0)
        self._warned_reply = None
        self._warned_70 = False
        self._warned_90 = False

    def _effective_budget(self, agent) -> float:
        """当前档位生效的预算。未配置 artifact 预算时与改造前完全一致。"""
        if self._artifact_budget > 0:
            try:
                if dp.is_artifact(agent):
                    return max(self._base_budget, self._artifact_budget)
            except Exception:
                pass
        return self._base_budget

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        mw_key = await self.get_middleware_key()
        reply_id = agent.state.reply_id
        if reply_id != self._warned_reply:
            self._warned_reply = reply_id
            self._warned_70 = False
            self._warned_90 = False
        store = agent.state.middle_context.get(mw_key, {})
        used = store.get(reply_id, 0)

        # 父类的硬截止读的就是 self.token_budget，这里按档位改写后再委派；
        # 每次推理都重算，档位中途变化（CreatePlan 声明）也能立即生效。
        budget = self._effective_budget(agent)
        self.token_budget = budget

        if budget > 0:
            pct = used / budget
            if pct >= 1.0:
                # 预算耗尽：直接发 __turn_timeout__ 强制停止，不再调模型。
                # 父类在 100% 时只设 tool_choice=none，但 LLM 仍可生成纯文本
                # 回复继续推理（实测预算耗尽后又多跑了 6 轮 Bash+chat）。
                logger.warning(
                    "[budget] session=%s reply=%s 预算耗尽(%.0f%%)，强制停止",
                    getattr(agent.state, "session_id", "?"), reply_id, pct * 100,
                )
                yield {"__turn_timeout__": True}
                return
            if pct >= 0.9 and not self._warned_90:
                self._inject_hint(agent, _WARN_90)
                self._warned_90 = True
            elif pct >= 0.7 and not self._warned_70:
                self._inject_hint(agent, _WARN_70)
                self._warned_70 = True

        # Delegate to parent for the 100% hard cutoff (tool_choice=none)
        async for event in super().on_reasoning(agent, input_kwargs, next_handler):
            yield event

    @staticmethod
    def _inject_hint(agent: "Agent", text: str) -> None:
        hint = HintBlock(hint=text)
        ctx = agent.state.context
        if ctx and ctx[-1].role == "assistant" and ctx[-1].name == agent.name:
            ctx[-1].content.append(hint)
        else:
            ctx.append(
                AssistantMsg(
                    id=agent.state.reply_id,
                    name=agent.name,
                    content=[hint],
                ),
            )
