"""
数据库初始化脚本

运行此脚本来：
1. 创建所有数据库表
2. 创建默认管理员账号
3. 创建示例Agent
4. 注册Skills

运行命令:
  python init_db.py
"""

from app.database import SessionLocal, init_db
from app.models.user import User
from app.models.agent import Agent
from app.models.user_agent_access import UserAgentAccess
from app.models.skill import Skill
from app.core.security import get_password_hash
from app.core.password_policy import DEFAULT_INITIAL_PASSWORD
from app.config import settings
from datetime import datetime, timezone

def init_database():
    """初始化数据库"""
    print("正在初始化数据库...")
    
    # 创建所有表
    init_db()
    print("[OK] 数据库表创建完成")
    
    # 创建数据库会话
    db = SessionLocal()
    
    try:
        # ========== 1. 创建管理员账号 ==========
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            admin = User(
                username="admin",
                account="admin",
                password_hash=get_password_hash(DEFAULT_INITIAL_PASSWORD),
                email="admin@example.com",
                phone="18888888888",
                role="admin",
                is_active=True,
                must_change_password=True,
                password_initialized_at=datetime.now(timezone.utc),
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            print("[OK] 管理员账号创建完成")
            print("    账号: admin")
            print(f"    初始密码: {DEFAULT_INITIAL_PASSWORD}（首次登录必须修改）")
        else:
            print("[INFO] 管理员账号已存在")
        
        # ========== 2. 创建示例Agent（工具助手 dialog + 数据分析师 task）==========
        # 注意：存量库升级请运行 migrate_agents_v2.py，两处 prompt 需保持一致
        existing_agent = db.query(Agent).filter(Agent.name == "工具助手").first()
        if not existing_agent:
            # 创建工具助手
            agent1 = Agent(
                name="工具助手",
                description="轻量工具助手：生成报告、识别 PDF、转 Markdown 等文件与工具类任务",
                agent_type="dialog",
                model_config_name=settings.MODEL_NAME,
                system_prompt="你是\"工具助手\"，负责帮助用户完成轻量级的文件与工具类任务。请遵循以下原则：\n1. 擅长使用技能处理文件：识别/填写 PDF、把文档转换为 Markdown、生成各类报告\n2. 执行任务前先用一两句话说明你的计划，再调用工具\n3. 结果表述简洁清晰；生成了输出文件时，告诉用户文件的用途\n4. 用中文回答所有问题",
                is_active=True
            )
            db.add(agent1)
            
            # 创建数据分析师
            agent2 = Agent(
                name="数据分析师",
                description="数据分析师：数据导出、数据分析与代码生成",
                agent_type="task",
                model_config_name=settings.MODEL_NAME,
                system_prompt="你是\"数据分析师\"，负责数据导出、数据分析与代码生成类任务。请遵循以下原则：\n1. 帮助用户导出数据，并对导出数据做分布、异常、趋势等分析\n2. 编写代码时先说明思路，保证代码可运行、结论可复现\n3. 数据结论必须有依据，关键处给出具体数字\n4. 用中文回答所有问题",
                is_active=True
            )
            db.add(agent2)
            db.commit()
            db.refresh(agent1)
            db.refresh(agent2)
            
            # 给管理员授予这两个Agent的访问权限
            access1 = UserAgentAccess(user_id=admin.id, agent_id=agent1.id, access_level="execute")
            access2 = UserAgentAccess(user_id=admin.id, agent_id=agent2.id, access_level="execute")
            db.add(access1)
            db.add(access2)
            db.commit()
            
            print("[OK] 示例Agent创建完成")
            print("    - 工具助手")
            print("    - 数据分析师")
        else:
            print("[INFO] 示例Agent已存在")
        
        # ========== 3. 注册Skills ==========
        existing_skill = db.query(Skill).filter(Skill.name == "calculator").first()
        if not existing_skill:
            skills_data = [
                {
                    "name": "calculator",
                    "display_name": "数学计算器",
                    "description": "计算数学表达式，支持加减乘除和括号运算",
                    "required_role": "viewer"
                },
                {
                    "name": "translator",
                    "display_name": "文本翻译器",
                    "description": "中英文互译工具",
                    "required_role": "operator"
                },
                {
                    "name": "data_formatter",
                    "display_name": "数据格式化器",
                    "description": "JSON/CSV/表格数据格式转换",
                    "required_role": "admin"
                },
            ]
            for sd in skills_data:
                skill = Skill(**sd)
                db.add(skill)
            db.commit()
            print("[OK] 3个Skill注册完成")
            print("    - 数学计算器 (viewer)")
            print("    - 文本翻译器 (operator)")
            print("    - 数据格式化器 (admin)")
        else:
            print("[INFO] Skill已存在")
        
        print()
        print("=" * 50)
        print("  数据库初始化完成！")
        print("=" * 50)
        print()
        print("下一步:")
        print("  1. 在 .env 文件中配置 AI_GATEWAY_API_KEY（不要提交真实密钥）")
        print("  2. 运行: python -m uvicorn app.main:app --reload")
        print("  3. 访问: http://localhost:8000/docs 查看API文档")
        print()
        
    finally:
        db.close()

if __name__ == "__main__":
    init_database()
