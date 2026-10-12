"""架构构建器注册表：agent_type -> 构建器实例。

新增 Agent 架构的步骤：
1. 在本目录新增一个脚本（如 xxx.py），继承 AgentBuilderBase 覆写钩子；
2. 在 _BUILDERS 里注册 agent_type -> 实例；
3. 数据库 agents 表把对应 Agent 的 agent_type 设为该键。
未注册的 agent_type 回退到 dialog 构建器，保证旧数据可用。
"""

from app.agents.architectures.dialog import ToolAssistantBuilder
from app.agents.architectures.task import DataAnalystBuilder


_BUILDERS = {
    "dialog": ToolAssistantBuilder(),   # 工具助手
    "task": DataAnalystBuilder(),       # 数据分析师
}

_DEFAULT_BUILDER = _BUILDERS["dialog"]


def get_builder(agent_type: str):
    """按 agent_type 取构建器实例；未知类型回退默认（dialog）。"""
    return _BUILDERS.get(agent_type, _DEFAULT_BUILDER)
