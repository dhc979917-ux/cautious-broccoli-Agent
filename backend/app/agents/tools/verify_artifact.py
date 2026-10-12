"""VerifyArtifact 工具：交付前的一次性产物验证（做完再看）。

存在的理由：以前提示词禁止验证，是因为模型自己用 shell 摸验证要花 5~10 轮
（反复试跑、curl 探端口、逐段回读）。把静态检查与无头渲染封装进一个工具后，
同样的覆盖只要 1 轮，验证从"浪费"变成"划算"，于是可以重新要求它。

边界与安全：
- 只接受会话输出目录内的路径；``..`` 逃逸一律拒绝（映射逻辑复用
  workspace.backend_path_of，与 Read/Write 同一套路径口径，不另立规则）；
- 只验证文本类前端产物（html/svg 等），二进制产物直接拒，不白跑；
- 验证服务自身不可用（缺 node / 缺 Playwright）时降级为静态检查并在结论里
  写明降级原因，绝不抛异常阻断交付。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.state import AgentState


class VerifyArtifact(ToolBase):
    """对产物做分层验证，返回紧凑的结构化结论。"""

    name: str = "VerifyArtifact"

    description: str = (
        "Verify a delivered web artifact BEFORE handing it over. One call runs "
        "static checks (undefined CSS variables, dangling getElementById targets, "
        "inline handlers calling undefined functions, duplicate ids, unclosed tags, "
        "script syntax, external CDN references that break offline/intranet use, "
        "zero-height chart containers) and, when a headless browser is available, "
        "a real render pass (page errors, console errors, failed requests, "
        "zero-size canvases, horizontal overflow, click probing). "
        "Returns a compact verdict: ok / errors / warnings / externals / "
        "layout_risk / degraded. "
        "Call it exactly once after the artifact is written, fix reported errors "
        "with targeted Edit (at most 2 rounds), then re-verify once and deliver. "
        "Do NOT hand-roll verification with shell commands - this tool already "
        "does it in one round. Path must be inside the session output directory."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "产物路径（相对会话工作目录或绝对路径均可）。",
            },
            "render": {
                "type": "boolean",
                "description": "是否执行无头渲染检查（默认 true；只想快速静态检查时传 false）。",
            },
        },
        "required": ["path"],
    }

    # 验证会起子进程（node --check / 无头浏览器），不允许并发；只读不写产物
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    is_state_injected: bool = True
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: Optional[str] = None

    def __init__(self, output_dir: str) -> None:
        # 会话输出目录（后端侧路径）：既做路径映射基准，也做越界守卫边界
        self._output_dir = str(output_dir or "")

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """只读验证，无副作用，放行（越界由 call 内部拒绝，不走权限层）。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="VerifyArtifact is read-only and always allowed.",
        )

    async def call(
        self,
        _agent_state: AgentState,
        path: str,
        render: bool = True,
    ) -> ToolChunk:
        from app.agents import quality_metrics, workspace
        from app.services.artifact_verify import service as verify_service

        raw = (path or "").strip()
        if not raw:
            return self._chunk("VerifyArtifact 调用无效：path 为空。", error=True)

        if not os.path.isabs(raw):
            raw = os.path.join(self._output_dir, raw)
        backend_path = workspace.backend_path_of(raw, self._output_dir)
        if not backend_path:
            return self._chunk(
                "已拒绝：路径越出本次会话的输出目录。只能验证本会话产出的文件。",
                error=True,
            )
        if not os.path.isfile(backend_path):
            return self._chunk(
                f"文件不存在：{path}（后端侧解析为 {backend_path}）。"
                "请确认文件已写入且路径正确。",
                error=True,
            )

        try:
            verdict = await verify_service.verify(backend_path, render=bool(render))
        except Exception as e:  # 验证层自身异常不能变成任务失败
            return self._chunk(
                f"验证执行异常（不影响交付，但请在回复中说明未验证）：{e}",
                error=True,
            )

        # 指标记账：供 done 事件与监控页聚合（键名集中在 quality_metrics）
        try:
            quality_metrics.record(
                _agent_state,
                verify_calls=1,
                verify_errors=len(verdict.errors),
                verify_tier=verdict.tier,
                verify_ok=verdict.ok,
                verify_layout_risk=verdict.layout_risk,
                verify_degraded=";".join(verdict.degraded) or None,
                last_verified_path=os.path.basename(backend_path),
            )
        except Exception:
            pass

        label = f"：{os.path.basename(backend_path)}"
        result_text = verify_service.format_text(verdict, label)
        # 验证通过时追加交付提示：明确告诉模型文件路径与交付方式，
        # 避免模型只说"已就绪"却不触发下载机制（用户要多次催才拿到产物）
        if verdict.ok:
            result_text += (
                f"\n\n产物已验证通过，可交付。文件路径：{path}"
                "\n请在最终回复中明确告知用户文件路径，"
                "系统会自动检测并生成下载链接。"
            )
        else:
            result_text += (
                "\n\n验证未通过，请修复后重新验证。"
                "修复后再次调用 VerifyArtifact 验证，通过后再交付。"
            )
        return self._chunk(result_text)

    @staticmethod
    def _chunk(text: str, error: bool = False) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(type="text", text=text)],
            state=ToolResultState.ERROR if error else ToolResultState.SUCCESS,
        )
