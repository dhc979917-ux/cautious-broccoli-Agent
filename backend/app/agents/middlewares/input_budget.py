"""ContextInputLimitMiddleware: hard cap on per-call input size.

AgentScope's native compression waits for a ratio of the model context window
(~55% after tuning). Long tool-heavy tasks can still grow each round's prompt
far beyond what the task actually needs before that threshold is reached.

This middleware adds an absolute input-side guard: before reasoning, estimate
the serialized context tokens; if they exceed CONTEXT_INPUT_HARD_LIMIT_TOKENS,
force agent.compress_context() first. It limits the size of a single model
call, not the number of rounds or the reply token budget.
"""

import logging
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase
from app.config import settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agentscope.agent import Agent


def estimate_context_tokens(ctx) -> int:
    """Rough token estimate: characters / 3 (same heuristic as headroom)."""
    chars = 0
    for msg in ctx or []:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            chars += len(getattr(block, "text", "") or "")
            chars += len(getattr(block, "thinking", "") or "")
            tool_input = getattr(block, "input", None)
            if tool_input:
                chars += len(str(tool_input))
            output = getattr(block, "output", None)
            if isinstance(output, list):
                for sub in output:
                    chars += len(getattr(sub, "text", "") or "")
            elif isinstance(output, str):
                chars += len(output)
    return chars // 3


def record_input_usage(agent, actual_tokens: int) -> None:
    '''Calibrate each agent's static prompt/tool overhead from reported usage.'''
    estimated = getattr(agent, '_input_budget_pending_estimate', None)
    if estimated is not None and actual_tokens > 0:
        agent._input_budget_overhead = max(0, int(actual_tokens) - estimated)
    agent._input_budget_pending_estimate = None


class ContextInputLimitMiddleware(MiddlewareBase):
    """Force context compression when a single call's input is too large."""

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        limit = int(getattr(settings, "CONTEXT_INPUT_HARD_LIMIT_TOKENS", 0) or 0)
        if limit > 0:
            ctx = getattr(getattr(agent, "state", None), "context", None) or []
            context_estimate = estimate_context_tokens(ctx)
            est = context_estimate + int(getattr(agent, '_input_budget_overhead', 0) or 0)
            if est > limit:
                try:
                    before = len(ctx)
                    await agent.compress_context()
                    after = len(agent.state.context or [])
                    logger.info(
                        "[input_limit] session=%s est_tokens=%d > %d, "
                        "compressed msgs %d -> %d",
                        getattr(agent.state, "session_id", "?"),
                        est, limit, before, after,
                    )
                except Exception as e:
                    # Compression failure must never block the reply.
                    logger.warning("[input_limit] compress failed (ignored): %s", e)
            agent._input_budget_pending_estimate = estimate_context_tokens(
                getattr(agent.state, 'context', None)
            )
        async for event in next_handler(**input_kwargs):
            yield event
