"""SessionBudgetMiddleware: session-level cumulative token guard.

Tracks LLM rounds per session and injects progressive warnings when the
round count exceeds thresholds. Complements ProactiveBudgetMiddleware
(which controls per-reply budget) by catching sessions that accumulate
high token costs across many tool-call rounds.

Thresholds (configurable via settings):
- SOFT_LIMIT (default 12): gentle reminder to prioritise and wrap up
- HARD_LIMIT (default 18): force immediate finish with current results
"""

from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase
from agentscope.message import AssistantMsg, HintBlock

from app.agents import delivery_profile as dp
from app.config import settings

if TYPE_CHECKING:
    from agentscope.agent import Agent

_SOFT_LIMIT = 12
_HARD_LIMIT = 18

# "跳过非必要的验证"会把交付质量排在省 token 之后，与质量闭环目标冲突；
# 真正该省的是重复读取与探索性搜索。
_WARN_SOFT = (
    "当前会话已进行较多轮工具调用，累计 token 消耗较高。"
    "请优先完成核心交付物，跳过重复读取和探索性搜索。"
    "注意：需求采集与交付前验证不属于可跳过项。"
)
_WARN_HARD = (
    "当前会话工具调用轮次已达上限。立即用已有成果收尾，"
    "不要再调用工具，直接给出最终回复。"
    "若产物未经 VerifyArtifact 验证，必须在回复中显式写明「未验证」及原因。"
)


class SessionBudgetMiddleware(MiddlewareBase):
    """Session-level cumulative round guard with progressive warnings.

    档位感知：构建型任务（artifact 档）的正常流程是
    采集 1 轮 + 规划 1 轮 + 骨架/填充 3~5 轮 + 验证 1 轮 + 修复 ≤2 轮 + 复验 1 轮
    ≈ 8~11 轮，默认 12/18 的上限几乎没有余量，会在验证前就触发收尾警告。
    因此该档位单独放宽；未配置时行为与改造前完全一致。
    """

    def __init__(
        self,
        soft_limit: int = _SOFT_LIMIT,
        hard_limit: int = _HARD_LIMIT,
        artifact_soft_limit: int = 0,
        artifact_hard_limit: int = 0,
    ) -> None:
        self._soft = soft_limit
        self._hard = hard_limit
        self._artifact_soft = artifact_soft_limit or int(
            getattr(settings, "SESSION_ROUNDS_SOFT_ARTIFACT", 0) or 0
        )
        self._artifact_hard = artifact_hard_limit or int(
            getattr(settings, "SESSION_ROUNDS_HARD_ARTIFACT", 0) or 0
        )
        self._warned_soft: set = set()
        self._warned_hard: set = set()

    def _limits(self, agent) -> tuple[int, int]:
        """当前档位的软/硬轮数上限（artifact 档取更大值）。"""
        soft, hard = self._soft, self._hard
        try:
            if dp.is_artifact(agent):
                soft = max(soft, self._artifact_soft) if self._artifact_soft else soft
                hard = max(hard, self._artifact_hard) if self._artifact_hard else hard
        except Exception:
            pass
        return soft, hard

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        sid = str(getattr(agent.state, "session_id", "") or "")
        mw_key = "session_budget"
        store = agent.state.middle_context.setdefault(mw_key, {})
        rounds = store.get("rounds", 0)
        soft, hard = self._limits(agent)

        if hard and rounds >= hard and sid not in self._warned_hard:
            self._inject_hint(agent, _WARN_HARD)
            self._warned_hard.add(sid)
        elif soft and rounds >= soft and sid not in self._warned_soft:
            self._inject_hint(agent, _WARN_SOFT)
            self._warned_soft.add(sid)

        async for event in next_handler(**input_kwargs):
            yield event

        # Increment after the round completes
        store["rounds"] = rounds + 1

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
