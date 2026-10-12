"""数据分析师 架构定义（agent_type="task"）。

定位：FDC 数据导出、导出数据情况分析和代码生成等重任务。
特点：低温度保证代码与数据分析的确定性，thinking 开启且预算更大，
允许并行工具调用（多个独立导出/查询可同时跑），token 预算给足上限。

温度 0.2、thinking_budget 8192、token 预算 200 万（settings.TOKEN_BUDGET_TASK）、工具并行
ReAct 轮次不设平台级上限：唯一的轮次闸是 Agent 自己配置的 max_react_iters，
未配置即不限，实际停止条件只有 token 预算（见 base.py 的 get_react_config）。

"""

from agentscope.agent._config import ContextConfig
from agentscope.model import OpenAIChatModel

from app.agents.architectures.base import AgentBuilderBase
from app.config import settings


class DataAnalystBuilder(AgentBuilderBase):
    """数据分析师构建器。"""

    BUILTIN_SKILLS_DIR = "skills_task"

    def get_parameters(self) -> "OpenAIChatModel.Parameters":
        return OpenAIChatModel.Parameters(
            temperature=0.2,
            max_tokens=16384,
            parallel_tool_calls=True,
        )

    def get_token_budget(self) -> float:
        # 单轮回复的加权 token 预算（不是会话级）。默认 200 万，可用环境变量覆盖。
        return float(settings.TOKEN_BUDGET_TASK)

    def get_context_config(self) -> ContextConfig:
        return ContextConfig(
            trigger_ratio=0.55,
            reserve_ratio=0.1,
            tool_result_limit=2000,
        )

    def extra_prompt(self, output_dir: str, has_skills: bool, in_docker: bool = False) -> str:
        """在公共提示之上追加：本地应用交付规范（写代码 → 本地跑起来 → 给链接）。"""
        base = super().extra_prompt(output_dir, has_skills, in_docker=in_docker)
        return base + f"""

## 本地应用交付规范
当用户要求开发可运行的系统/工具（如日常管理系统、看板、小 Web 应用）时：
1. 项目代码统一放在 {output_dir}/projects/<项目名>/ 目录下
2. 优先使用轻量常见技术栈（Flask/FastAPI + SQLite，或纯静态页面），确保一次跑通
3. 需要依赖时先用 pip 安装；启动服务必须用后台方式，例如：
4. 端口优先选 8100~8199 的空闲端口，启动后验证端口已在监听
5. 回复中给出访问链接 http://localhost:<端口>，并简要说明如何停止服务
6. 端口在监听只说明进程活着，不代表页面正确。交付前仍须按「交付前验证规则」
   调用 VerifyArtifact 检查产物（静态缺陷 + 无头渲染），报出的 error 修完再交付
"""
