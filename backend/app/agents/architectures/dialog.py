"""工具助手架构定义（agent_type="dialog"）。

定位：轻量工具型任务——生成报告、用 pdf 技能识别 PDF、转 markdown 等。
特点：温度偏高（输出更自然）、思维链开启但预算适中、工具串行执行。
工具助手温度 0.7、thinking_budget 4096、token 预算 100 万（settings.TOKEN_BUDGET_DIALOG）、工具串行
ReAct 轮次不设平台级上限：唯一的轮次闸是 Agent 自己配置的 max_react_iters，
未配置即不限，实际停止条件只有 token 预算（见 base.py 的 get_react_config）。

本文件只描述"这个架构和别人的差异"，公共构建流程见 base.py。
"""

from agentscope.agent._config import ContextConfig
from agentscope.model import OpenAIChatModel

from app.agents.architectures.base import AgentBuilderBase
from app.config import settings


class ToolAssistantBuilder(AgentBuilderBase):
    """工具助手：报告生成 / PDF 识别 / markdown 转换等轻量任务。"""

    BUILTIN_SKILLS_DIR = "skills_dialog"

    def get_parameters(self):
        return OpenAIChatModel.Parameters(
            temperature=0.7,
            max_tokens=8192,
            parallel_tool_calls=True,
        )

    def get_token_budget(self) -> float:
        # 单轮回复的加权 token 预算（不是会话级）。默认 100 万，可用环境变量覆盖。
        return float(settings.TOKEN_BUDGET_DIALOG)

    def get_context_config(self) -> ContextConfig:
        return ContextConfig(
            trigger_ratio=0.55,
            reserve_ratio=0.1,
            tool_result_limit=2000,
        )

    def get_soft_round_limit(self) -> int:
        """软轮次上限（P2-3）：dialog 默认 25 轮注入收尾提醒（不硬停）。
        硬停仍归 Agent 自配的 max_react_iters / token 预算管；0 = 关闭。"""
        return int(getattr(settings, "DIALOG_SOFT_ROUND_LIMIT", 0) or 0)
