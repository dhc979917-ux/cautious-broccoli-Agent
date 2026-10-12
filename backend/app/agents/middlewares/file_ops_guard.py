"""file_ops guard: ensure reset_tools always includes file_ops.

Problem: AgentScope reset_tools is a REPLACE operation. The model sometimes
calls reset_tools to activate skill groups but forgets file_ops, causing
Read/Edit/Glob/Grep to vanish from the available tool list. Subsequent
calls fail with ToolNotFoundError. The system prompt already warns about
this, but model compliance is unreliable (session 364 round 2 reproduced).

Approach: use the on_acting hook to intercept reset_tools calls. If the
model did not explicitly mention file_ops (neither true nor false), inject
file_ops: true automatically. If the model explicitly set file_ops: false,
respect that choice (it may want to conserve context space).
Does not modify events, context, or swallow exceptions. Coexists with
tracing, budget, and compression middlewares without interference.
"""

import json
import logging
from typing import Any, AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)


class FileOpsGuardMiddleware(MiddlewareBase):
    """Intercept reset_tools in on_acting, auto-inject file_ops: true."""

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        name = self._get_name(tool_call)

        if name == "reset_tools":
            self._inject_file_ops(tool_call)

        async for item in next_handler(**input_kwargs):
            yield item

    @staticmethod
    def _get_name(tool_call: Any) -> str:
        """Extract tool name from ToolCallBlock (handles dict and object)."""
        if isinstance(tool_call, dict):
            return str(tool_call.get("name") or "")
        return str(getattr(tool_call, "name", "") or "")

    @staticmethod
    def _inject_file_ops(tool_call: Any) -> None:
        """If file_ops is not mentioned in reset_tools input, inject it."""
        if isinstance(tool_call, dict):
            raw_input = tool_call.get("input")
        else:
            raw_input = getattr(tool_call, "input", None)

        if raw_input is None:
            return

        # Parse input (JSON string or already-parsed dict)
        if isinstance(raw_input, str):
            try:
                args = json.loads(raw_input)
            except (json.JSONDecodeError, TypeError):
                return
        elif isinstance(raw_input, dict):
            args = raw_input
        else:
            return

        if not isinstance(args, dict):
            return

        # Only inject when model did not mention file_ops at all
        if "file_ops" not in args:
            args["file_ops"] = True
            logger.info(
                "[file_ops_guard] reset_tools missing file_ops, auto-injected"
            )
            # Write back: serialize if original was a string
            if isinstance(raw_input, str):
                new_input = json.dumps(args, ensure_ascii=False)
                if isinstance(tool_call, dict):
                    tool_call["input"] = new_input
                else:
                    try:
                        tool_call.input = new_input
                    except (AttributeError, TypeError):
                        pass
