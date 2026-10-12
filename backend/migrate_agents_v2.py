"""一次性迁移脚本：把旧的种子 Agent 升级为新的双 Agent 配置（可安全重复执行）。

变更内容：
1. "客服助手" 更名为 "工具助手"（agent_type=dialog），更新描述与 system_prompt
2. "数据分析师"（agent_type=task）更新描述与 system_prompt
3. 两者的模型统一设为平台默认模型 settings.MODEL_NAME（数据库 model_config_name，运行时真实生效）
4. 确保 admin 拥有两个 Agent 的访问权限

用法（在 backend 目录下）：
    python migrate_agents_v2.py

注意：init_db.py 的新库种子已同步为本配置，两处 prompt 修改需保持一致。
"""

from app.database import SessionLocal
from app.models.agent import Agent
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.config import settings


# 模型名以平台配置为唯一来源：改默认模型只需动 .env / config.py，不必再改本脚本
MODEL_NAME = settings.MODEL_NAME

TOOL_ASSISTANT_PROMPT = (
    '你是"工具助手"，负责帮助用户完成轻量级的文件与工具类任务。请遵循以下原则：\n'
    "1. 擅长使用技能处理文件：识别/填写 PDF、把文档转换为 Markdown、生成各类报告\n"
    "2. 执行任务前先用一两句话说明你的计划，再调用工具\n"
    "3. 结果表述简洁清晰；生成了输出文件时，告诉用户文件的用途\n"
    "4. 用中文回答所有问题"
)

DATA_ANALYST_PROMPT = (
    '你是"数据分析师"，负责数据导出、数据分析与代码生成类任务。请遵循以下原则：\n'
    "1. 帮助用户导出 FDC 数据，并对导出数据做分布、异常、趋势等分析\n"
    "2. 编写代码时先说明思路，保证代码可运行、结论可复现\n"
    "3. 数据结论必须有依据，关键处给出具体数字\n"
    "4. 用中文回答所有问题"
)


def _upsert_agent(db, old_names, new_name, description, agent_type, system_prompt):
    """按旧名/新名查找 Agent，找不到则新建；统一刷新为新配置。"""
    agent = db.query(Agent).filter(Agent.name.in_(old_names)).first()
    if not agent:
        agent = Agent(is_active=True)
        db.add(agent)
    agent.name = new_name
    agent.description = description
    agent.agent_type = agent_type
    agent.model_config_name = MODEL_NAME
    agent.system_prompt = system_prompt
    return agent


def migrate():
    db = SessionLocal()
    try:
        tool = _upsert_agent(
            db,
            old_names=["客服助手", "工具助手"],
            new_name="工具助手",
            description="轻量工具助手：生成报告、识别 PDF、转 Markdown 等文件与工具类任务",
            agent_type="dialog",
            system_prompt=TOOL_ASSISTANT_PROMPT,
        )
        analyst = _upsert_agent(
            db,
            old_names=["数据分析师"],
            new_name="数据分析师",
            description="数据分析师：FDC 数据导出、数据分析与代码生成",
            agent_type="task",
            system_prompt=DATA_ANALYST_PROMPT,
        )
        db.commit()
        db.refresh(tool)
        db.refresh(analyst)
        print(f"[OK] 工具助手 id={tool.id} type={tool.agent_type} model={tool.model_config_name}")
        print(f"[OK] 数据分析师 id={analyst.id} type={analyst.agent_type} model={analyst.model_config_name}")

        # 给 admin 授权（幂等：已有记录则跳过）
        admin = db.query(User).filter(User.username == "admin").first()
        if admin:
            for agent in (tool, analyst):
                exists = db.query(UserAgentAccess).filter(
                    UserAgentAccess.user_id == admin.id,
                    UserAgentAccess.agent_id == agent.id,
                ).first()
                if not exists:
                    db.add(UserAgentAccess(
                        user_id=admin.id, agent_id=agent.id, access_level="execute"
                    ))
            db.commit()
            print("[OK] admin 访问权限已确认")
        else:
            print("[WARN] 未找到 admin 账号，跳过授权（请先运行 init_db.py）")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
