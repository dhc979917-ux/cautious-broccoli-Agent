"""ThinkingBudgetMiddleware：thinking 预算按交付档位降档（P2-3）。

为什么需要（2026-09-22 大纲实证）：
dialog 架构对所有任务常开 thinking_budget=4096——闲聊一句"你好"也要付思考
预算；而 output token 按 ×2 权重计费（预算口径），是单价最贵的部分。
构建型（artifact）任务的思考量需求真实存在，问答/文档类任务则不然。

做法：每轮推理前按 BuildProfile 的档位判定结果调整 agent.model.parameters
.thinking_budget（DashScope/OpenAI 兼容模型每次调用现读该值，见
model/_dashscope/_model.py 的请求组装）。

安全性：model_factory 缓存共享模型实例——直接改共享实例的参数会串扰其它会话。
因此启用本中间件的架构必须用 fresh model（base.py 在 get_thinking_budgets()
非空时以 fresh=True 取模型，模型实例会话私有，改动不外溢）。
"""

import logging
from typing import AsyncGenerator, Callable, Optional, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

from app.agents import delivery_profile as dp

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)


class ThinkingBudgetMiddleware(MiddlewareBase):
    """按档位下调 thinking 预算：artifact 档用 default，其余档用 simple。"""

    def __init__(self, default_budget: int, simple_budget: int) -> None:
        self.default_budget = int(default_budget or 0)
        self.simple_budget = int(simple_budget or 0)
        self._applied: Optional[int] = None   # 当前已应用的值（避免重复赋值打日志）

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        try:
            self._apply(agent)
        except Exception as e:  # 调档失败不阻断推理
            logger.debug("[thinking_budget] 调档失败（忽略）: %s", e)
        async for event in next_handler(**input_kwargs):
            yield event

    def _apply(self, agent: "Agent") -> None:
        model = getattr(agent, "model", None)
        params = getattr(model, "parameters", None)
        if params is None or not hasattr(params, "thinking_budget"):
            return
        target = self.default_budget if dp.is_artifact(agent) else self.simple_budget
        if target <= 0 or target == self._applied:
            return
        old = getattr(params, "thinking_budget", None)
        if old == target:
            self._applied = target
            return
        params.thinking_budget = target
        self._applied = target
        logger.info(
            "[thinking_budget] session=%s 档位=%s thinking_budget %s→%d",
            getattr(getattr(agent, "state", None), "session_id", "?"),
            dp.get(agent),
            old, target,
        )
