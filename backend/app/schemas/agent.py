from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from pydantic import ConfigDict


'''Agent数据格式'''


# Agent列表响应
# 列表页只展示用户需要知道的最小信息集，减少数据传输量，加快页面加载
class AgentResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    agent_type: str
    is_active: bool
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

# Agent详情响应
# 点击某个 Agent 进入详情页时，需要更完整的信息
class AgentDetailResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    agent_type: str
    model_config_name: str
    system_prompt: Optional[str] = None
    # 扩展配置（如 {"builtin_skills": [...]} 附加内置技能）
    config: Optional[dict] = None
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

# 创建Agent请求（管理员用）
class CreateAgentRequest(BaseModel):
    name: str
    description: Optional[str] = None
    agent_type: str = "dialog" # 默认 "dialog"（对话型）。可以设为 "task"（任务型）或 "rag"（检索增强型）
    model_config_name: Optional[str] = None  # 不指定时继承同类型基底 Agent 的模型（service 层处理）
    system_prompt: Optional[str] = None
    # 扩展配置：{"builtin_skills": ["技能name", ...]}，附加内置技能（与基底架构的内置技能合并装载）
    config: Optional[dict] = None


# 更新Agent请求（管理员用）：仅允许修改名称/描述/系统提示词，其余字段不可改
class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model_config_name: Optional[str] = None  # 允许在编辑页改模型（此前不可改，配错了没法纠正）
    # 扩展配置增量合并：仅接受 mcp_servers / knowledge_bases（绑定的 MCP 服务 / 知识库 id 列表），
    # 其余已有键（如 builtin_skills）保持不变
    config: Optional[dict] = None
