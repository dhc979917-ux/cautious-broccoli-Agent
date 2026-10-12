"""AskUserChoice 工具（官方外部执行机制）。

标记 is_external_tool=True：模型调用时 AgentScope 不执行它，而是发出
RequireExternalExecutionEvent 暂停回复；manager.chat_stream 的恢复循环把
question/options 经 InteractionChannel 推给前端渲染选项卡，等待用户选择后
组装 ExternalExecutionResultEvent 再次调用 reply_stream 恢复执行。
"""
from typing import Any

from agentscope.tool._base import ToolBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)


class AskUserChoice(ToolBase):
    """向用户展示多个方案并等待用户选择（外部执行，由平台桥接）。"""

    name: str = "AskUserChoice"

    description: str = (
        "Present multiple feasible options to the user and wait for the user's "
        "choice before continuing. Use this tool whenever you need the user's "
        "input to move on: (1) a real decision point with multiple viable "
        "options whose preference you cannot determine yourself, or (2) the "
        "user's request is ambiguous, underspecified, or can be interpreted in "
        "multiple ways (e.g. missing task type, target, format, or scope). "
        "Never ask such questions as plain text; always use this tool instead. "
        "Provide 2-6 options; each option must have a short label and a "
        "one-sentence description covering its tradeoffs. "
        "After receiving the user's answer, you MUST proceed strictly according "
        "to the chosen option."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "呈现给用户的决策问题。用一两句话讲清楚背景、目标和需要用户拍板的点。"
                ),
            },
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "description": "2-6 个可行方案。",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "选项标题（简洁明确，如：方案A：沿用现有框架）",
                        },
                        "description": {
                            "type": "string",
                            "description": "选项说明：做法与优缺点（一两句话）",
                        },
                    },
                    "required": ["label", "description"],
                },
            },
        },
        "required": ["question", "options"],
    }

    # 外部执行工具：agent 不直接执行，暂停回复后由平台（WebSocket 桥）处理；
    # 等待用户输入可能长达 10 分钟，不允许并发；只读（不产生副作用）
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    is_external_tool: bool = True

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """向用户征求决策不产生副作用，始终放行。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="AskUserChoice is always allowed.",
        )