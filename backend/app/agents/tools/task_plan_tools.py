"""先规划后执行的任务计划工具（替代 AgentScope 原生 TaskCreate/TaskUpdate）。

设计目标（编码约束，不依赖提示词）：
- CreatePlan：一次性创建全部步骤，同一回复内只允许调用一次；
- AdvanceTask：完成当前步骤并推进到下一个，强制顺序执行；
- GetCurrentTask：查看当前步骤（供长任务中途找回状态）。

与 AgentScope 原生 Task 体系完全兼容：操作同一个
``agent.state.tasks_context.tasks``，前端计划面板与
``_inject_runtime_state`` 的任务快照无需任何改动。
"""

from typing import Any, Optional

from pydantic import BaseModel, Field

from agentscope.tool import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.state import AgentState, Task
from agentscope.permission import (
    PermissionContext,
    PermissionDecision,
    PermissionBehavior,
)
from agentscope.message import TextBlock, ToolResultState


class _PlanStep(BaseModel):
    subject: str = Field(description="步骤标题（祈使句，如：解析源文件）")
    description: str = Field(description="该步骤要做什么")


class _CreatePlanParams(BaseModel):
    steps: list[_PlanStep] = Field(
        description="全部步骤的列表，按执行顺序排列",
        min_length=1,
    )
    # 交付档位：决定平台是否启用需求采集与交付前验证。设为可选而非必填，
    # 是为了不破坏既有调用（模型漏填时退回关键词自动判定，不会白耗一轮报错）。
    delivery_kind: Optional[str] = Field(
        default=None,
        description=(
            # 判据写在这里而不是常驻系统提示词：只有真的调用 CreatePlan 时才需要看到，
            # 未激活 task_mgmt 的会话零增量（见 quality_prompts.DELIVERY_KIND_GUIDE）
            "本次任务的交付物类型，三选一："
            "artifact=可打开或可运行的产物（页面/看板/大屏/Web 应用/脚本工具/服务/交互 demo）；"
            "document=文档类交付（docx/xlsx/pptx/pdf/md 报告、总结、手册）；"
            "answer=只需回答、无文件交付（查询/解释/分析/对比/翻译）。"
            "同时涉及文档与可运行产物时填 artifact。"
            "它决定平台是否对你启用需求采集与交付前验证，请按实际交付物如实声明。"
        ),
    )


class _PlanToolBase(ToolBase):
    """计划工具公共基类：状态注入 + 默认放行权限。"""

    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_state_injected: bool = True
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed to be called.",
        )


def _fmt_task(t: Task) -> str:
    return f"[{t.id}] {t.subject}（{t.state}）: {t.description}"


class CreatePlan(_PlanToolBase):
    """一次性创建完整任务计划。"""

    name: str = "CreatePlan"

    description: str = (
        "为当前任务一次性创建完整的执行计划。把全部步骤作为列表传入，"
        "按执行顺序排列。计划创建后严格按顺序逐步执行，"
        "每完成一步调用 AdvanceTask 推进。"
        "只应在任务开始时调用一次；禁止拆成多次调用、禁止中途新建计划。"
        "简单任务（3 步以内）不需要创建计划，直接执行即可。"
    )

    input_schema: dict = _CreatePlanParams.model_json_schema()

    async def call(
        self,
        _agent_state: AgentState,
        steps: list[dict[str, Any]],
        delivery_kind: Optional[str] = None,
    ) -> ToolChunk:
        if not isinstance(_agent_state, AgentState):
            return ToolChunk(
                content=[TextBlock(text="CreatePlanError: AgentState 缺失")],
                state=ToolResultState.ERROR,
            )
        # 显式声明的档位粘住整个会话，优先于关键词自动判定
        if delivery_kind:
            try:
                from app.agents import delivery_profile as _dp

                _dp.declare(_agent_state, str(delivery_kind).strip().lower())
            except Exception:
                pass
        tasks = _agent_state.tasks_context.tasks
        # 编码约束：已有任务时禁止重复创建计划
        if any(t.state != "completed" for t in tasks):
            return ToolChunk(
                content=[TextBlock(
                    text="计划已存在且未全部完成，禁止重复创建。"
                    "请用 AdvanceTask 推进当前计划。",
                )],
                state=ToolResultState.ERROR,
            )
        try:
            max_numeric = 0
            for t in tasks:
                try:
                    max_numeric = max(max_numeric, int(t.id))
                except (ValueError, TypeError):
                    pass
            created = []
            for i, step in enumerate(steps):
                task = Task(
                    id=str(max_numeric + i + 1),
                    subject=step.get("subject", ""),
                    description=step.get("description", ""),
                    metadata={},
                )
                tasks.append(task)
                created.append(task)
            # 编码约束：创建即把第一步设为 in_progress，强制立即按计划执行
            if created:
                created[0].state = "in_progress"
            lines = [f"计划已创建（{len(created)} 步），当前执行第 1 步："]
            lines += [_fmt_task(t) for t in created]
            if delivery_kind:
                lines.append(f"交付档位：{str(delivery_kind).strip().lower()}")
            return ToolChunk(content=[TextBlock(text="\n".join(lines))])
        except Exception as e:
            return ToolChunk(
                content=[TextBlock(text=f"CreatePlanError: {e}")],
                state=ToolResultState.ERROR,
            )


class AdvanceTask(_PlanToolBase):
    """完成当前步骤并推进到下一步。"""

    name: str = "AdvanceTask"

    description: str = (
        "完成当前正在执行的步骤并推进到下一步。"
        "把当前 in_progress 的步骤标记为 completed，"
        "把下一个 pending 步骤标记为 in_progress 并返回其内容。"
        "每完成一个步骤调用一次，直到全部步骤完成。"
    )

    input_schema: dict = {"type": "object", "properties": {}}

    async def call(self, _agent_state: AgentState) -> ToolChunk:
        if not isinstance(_agent_state, AgentState):
            return ToolChunk(
                content=[TextBlock(text="AdvanceTaskError: AgentState 缺失")],
                state=ToolResultState.ERROR,
            )
        tasks = _agent_state.tasks_context.tasks
        if not tasks:
            return ToolChunk(
                content=[TextBlock(text="当前没有计划。如需规划请先用 CreatePlan。")],
            )
        # 找到当前 in_progress 的任务，标记完成
        current = next((t for t in tasks if t.state == "in_progress"), None)
        if current is not None:
            current.state = "completed"
        # 找下一个 pending
        nxt = next((t for t in tasks if t.state == "pending"), None)
        if nxt is None:
            return ToolChunk(content=[TextBlock(
                text="全部步骤已完成。请直接给出最终回复，不要再调用任务工具。",
            )])
        nxt.state = "in_progress"
        done_count = sum(1 for t in tasks if t.state == "completed")
        return ToolChunk(content=[TextBlock(
            text=f"已完成 {done_count}/{len(tasks)} 步。当前执行：\n{_fmt_task(nxt)}",
        )])


class GetCurrentTask(_PlanToolBase):
    """查看当前正在执行的步骤。"""

    name: str = "GetCurrentTask"

    description: str = (
        "查看当前正在执行的步骤（in_progress）。"
        "长任务中途需要确认进度时使用。"
    )

    input_schema: dict = {"type": "object", "properties": {}}

    async def call(self, _agent_state: AgentState) -> ToolChunk:
        if not isinstance(_agent_state, AgentState):
            return ToolChunk(
                content=[TextBlock(text="GetCurrentTaskError: AgentState 缺失")],
                state=ToolResultState.ERROR,
            )
        tasks = _agent_state.tasks_context.tasks
        if not tasks:
            return ToolChunk(content=[TextBlock(text="当前没有计划。")])
        current = next((t for t in tasks if t.state == "in_progress"), None)
        if current is None:
            nxt = next((t for t in tasks if t.state == "pending"), None)
            if nxt is None:
                return ToolChunk(content=[TextBlock(text="全部步骤已完成。")])
            return ToolChunk(content=[TextBlock(
                text=f"没有进行中的步骤，下一个待执行：\n{_fmt_task(nxt)}",
            )])
        return ToolChunk(content=[TextBlock(text=f"当前步骤：\n{_fmt_task(current)}")])
