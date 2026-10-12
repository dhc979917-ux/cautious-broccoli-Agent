from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from pydantic import ConfigDict


'''会话数据格式'''


# 创建会话请求：用户点击“新建对话”，前端发一个 POST 请求到 /api/sessions
# 没有 user_id！因为当前登录用户就是会话的所有者，由后端从 JWT 令牌中获取，不需要前端传。这防止了用户伪造别人的 user_id 来创建会话
class CreateSessionRequest(BaseModel):
    agent_id: int # 和哪个 Agent 对话（必填）
    title: Optional[str] = "新对话"  # 会话标题，可选，默认"新对话"

# 会话响应
# GET /api/sessions → 返回当前用户的所有会话列表（前端左侧聊天列表）
# POST /api/sessions 成功后返回新创建的会话
class SessionResponse(BaseModel):
    id: int
    agent_id: int # 关联的 Agent ID
    agent_name: Optional[str] = None # Agent 的名字。注意这个字段在数据库的 Session 表里不存在，是后端额外查询 Agent 表后填充的
    title: str
    status: str # 会话状态（"active" 或 "closed"）
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    model_config = ConfigDict(from_attributes=True)

# 消息响应：用户点击某个会话，前端调 GET /api/sessions/{id}/messages 拉取对话记录
class MessageResponse(BaseModel):
    id: int
    session_id: int # 属于哪个会话
    role: str # 谁说的（"user" / "agent" / "system"）
    content: str # 消息内容
    agent_name: Optional[str] = None # 当 role 是 "agent" 时，显示是哪个 Agent 回复的
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

# 发送消息请求:在聊天页输入框敲回车，前端调 POST /api/chat
class SendMessageRequest(BaseModel):
    content: str
    session_id: Optional[int] = None  # 如果不传，会用 agent_id 自动创建新会话
    agent_id: Optional[int] = None    # 仅在不传 session_id 时必填（新会话属于哪个 Agent）
    # 本次对话启用的技能白名单（可选）。与 WebSocket 链路语义一致：
    # None=使用角色可用的全部技能；传列表=与角色可用技能取交集
    selected_skills: Optional[List[str]] = None
    # 本次对话启用的 MCP 服务 id（可选）。与 WebSocket 链路语义一致：
    # None=Agent 绑定的全部启用 MCP；传列表=与 Agent 绑定集取交集
    selected_mcps: Optional[List[int]] = None
    # 本次对话使用的模型（可选）。不传则用 Agent 的默认配置模型
    model: Optional[str] = None
    # 结构化输出要求（可选）：JSON Schema dict，回复需符合该结构，结果随 done 事件的 structured_output 返回
    structured_schema: Optional[dict] = None

# 聊天响应（用于WebSocket）:前端与后端建立 WebSocket 连接，后端逐 token 推送 AI 的回复
class ChatResponse(BaseModel):
    type: str  # "message", "error", "end"
    content: Optional[str] = None # 消息内容（error 类型时是错误描述）
    session_id: Optional[int] = None 
    message_id: Optional[int] = None
