from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON
from sqlalchemy.sql import func
from app.database import Base


'''Agent列表'''


class Agent(Base):
    __tablename__ = "agents"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False) # Agent 名称，如"客服助手"、"数据分析师"
    description = Column(Text, nullable=True) # 描述信息，用 Text 类型
    #  Agent 类型，同时是架构路由键（agents/architectures/ 注册表按它选构建器）：
    #   - dialog：工具助手架构（轻量工具任务：报告生成、PDF 识别、转 markdown）
    #   - task：数据分析师架构（FDC 导出、数据分析、代码生成）
    #   - rag：检索增强型（预留类型，当前未实现，回退按 dialog 架构处理）
    agent_type = Column(String(50))  # dialog, task, rag
    # 用来存 AgentScope 的额外配置参数，比如温度（temperature）、top_p 等
    config = Column(JSON, nullable=True)  # AgentScope配置
    # 指定这个 Agent 用哪个大模型（运行时生效：架构构建器按此字段加载模型）
    # 默认留空：运行时回退 settings.MODEL_NAME（见 architectures/base.py 的 _model_name）。
    # 不写死模型名，避免平台默认模型升级后新 Agent 仍落在旧值上。
    model_config_name = Column(String(100), nullable=True)
    system_prompt = Column(Text, nullable=True)  # 定义了 Agent 的"人设"和行为准则
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
