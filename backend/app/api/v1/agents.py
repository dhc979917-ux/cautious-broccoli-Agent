from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.services.agent_service import AgentService
from app.schemas.agent import AgentResponse, AgentDetailResponse, CreateAgentRequest
from app.schemas.agent import UpdateAgentRequest
from app.core.deps import get_current_user, require_admin, require_perm, require_module_perm
from app.core.security import verify_password
from app.models.user import User as UserModel
from app.schemas.destroy import DestroyRequest
from app.models.user import User
from app.agents.manager import agent_manager
from fastapi.encoders import jsonable_encoder
from app.core.cache import cache_delete_pattern, cache_get, cache_set
from app.models.session import Session as ChatSession
from app.models.agent_governance import AgentPublication
from app.models.mcp_server import McpServer
from app.services.agent_versions import for_session
from app.services.model_access import available_catalog, allowed_models, effective_model
from app.services.agent_access import can_execute_agent

# Agent 列表缓存（Redis cache-aside，REDIS_URL 未配置时自动 no-op）
AGENT_LIST_CACHE_KEY = "agents:active"
AGENT_LIST_CACHE_TTL = 30


'''Agent 的列表、详情、创建、删除'''


router = APIRouter(prefix="/agents", tags=["Agent管理"])

# 具体路径必须在参数路径之前！
@router.get("/")
async def get_agents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取用户可用的Agent列表"""
    # ① 调第 5 层拿所有活跃 Agent ② 路由层自己做权限过滤——如果不是管理员，把 agent_permissions（字符串列表 ["1","2"]）转成整数列表，然后列表推导式只保留有权限的 Agent ③ 返回过滤后的列表。
    # 为什么权限过滤放在路由层而不是 Service 层？ 因为权限信息在 current_user 对象上（来自 JWT），而 AgentService 只接收 db，不接收用户对象。把权限逻辑放在路由层，Service 层保持纯粹的数据操作。
    agent_service = AgentService(db)
    all_agents = cache_get(AGENT_LIST_CACHE_KEY)
    if all_agents is None:
        all_agents = jsonable_encoder(agent_service.get_all_agents())
        cache_set(AGENT_LIST_CACHE_KEY, all_agents, ttl=AGENT_LIST_CACHE_TTL)
    
    if current_user.role != "admin":
        allowed_ids = [int(id) for id in current_user.agent_permissions]
        all_agents = [agent for agent in all_agents if agent["id"] in allowed_ids]
    
    return all_agents

@router.get("/all", response_model=List[AgentDetailResponse])
async def get_all_agents(
    current_user: User = Depends(require_module_perm("agent_manage")),
    db: Session = Depends(get_db)
):
    """获取所有Agent（包括已删除的，仅管理员）"""
    agent_service = AgentService(db)
    return agent_service.get_all_agents_include_deleted()

@router.get("/public")
async def get_public_agents(db: Session = Depends(get_db)):
    """公开接口：获取所有Agent列表（不需要登录，注册页面用）"""
    agent_service = AgentService(db)
    all_agents = cache_get(AGENT_LIST_CACHE_KEY)
    if all_agents is None:
        all_agents = jsonable_encoder(agent_service.get_all_agents())
        cache_set(AGENT_LIST_CACHE_KEY, all_agents, ttl=AGENT_LIST_CACHE_TTL)
    return [{"id": a["id"], "name": a["name"], "description": a["description"]} for a in all_agents]

# 参数路径放在最后
@router.get("/{agent_id}", response_model=AgentDetailResponse)
async def get_agent_detail(
    agent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取Agent详情(单个Agent))"""
    agent_service = AgentService(db)
    
    if current_user.role != "admin" and str(agent_id) not in current_user.agent_permissions:
        raise HTTPException(status_code=403, detail="无权访问该Agent")
    
    agent = agent_service.get_agent_by_id(agent_id)
    return agent

@router.get("/{agent_id}/runtime")
async def get_agent_runtime(agent_id: int, session_id: int | None = None,
                            current_user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    if not can_execute_agent(db, current_user.id, agent_id, current_user.role):
        raise HTTPException(403, '无权访问该Agent')
    agent = AgentService(db).get_agent_by_id(agent_id)
    publication = db.get(AgentPublication, agent_id)
    if session_id is not None:
        session = db.get(ChatSession, session_id)
        if session is None or session.user_id != current_user.id or session.agent_id != agent_id:
            raise HTTPException(404, '会话不存在')
        agent = for_session(db, agent, session_id)
    catalog = await available_catalog()
    config = agent.config or {}
    bound = config.get('mcp_servers') or []
    from app.services.mcp_access import allowed_ids
    bound = allowed_ids(db, current_user, bound)
    servers = db.query(McpServer.id).filter(
        McpServer.id.in_(bound), McpServer.enabled.is_(True), McpServer.is_deleted.is_(False)
    ).all()
    version_id = config.get('_agent_version_id') if session_id is not None else (
        publication.version_id if publication else None
    )
    return {'agent_id': agent_id, 'name': agent.name, 'version_id': version_id,
            'is_current': not publication or version_id == publication.version_id,
            'configured_model': agent.model_config_name,
            'default_model': effective_model(db, current_user, agent, catalog=catalog),
            'models': allowed_models(db, current_user, catalog),
            'mcp_servers': [server_id for (server_id,) in servers],
            'builtin_skills': config.get('builtin_skills') or []}


@router.post("/", response_model=AgentDetailResponse)
async def create_agent(
    data: CreateAgentRequest,
    current_user: User = Depends(require_perm("agent_manage.create")),
    db: Session = Depends(get_db)
):
    """创建新Agent（仅管理员）"""
    agent_service = AgentService(db)
    agent = agent_service.create_agent(data)
    cache_delete_pattern("agents:*")
    return agent

@router.put("/{agent_id}", response_model=AgentDetailResponse)
async def update_agent(
    agent_id: int,
    data: UpdateAgentRequest,
    current_user: User = Depends(require_perm("agent_manage.update")),
    db: Session = Depends(get_db)
):
    """更新Agent（仅管理员）：仅可修改名称/描述/系统提示词"""
    agent_service = AgentService(db)
    agent = agent_service.update_agent(agent_id, data)
    agent_manager.clear_cache()  # 失效缓存中的 Agent 实例，使系统提示词等修改立即生效
    cache_delete_pattern("agents:*")
    return agent

@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: int,
    current_user: User = Depends(require_perm("agent_manage.delete.soft")),
    db: Session = Depends(get_db)
):
    """删除Agent（仅管理员，软删除）"""
    agent_service = AgentService(db)
    agent_service.delete_agent(agent_id)
    agent_manager.clear_cache()  # 失效缓存中的 Agent 实例，防止已删除的 Agent 仍可对话
    cache_delete_pattern("agents:*")
    return {"message": "Agent已删除"}

@router.put("/{agent_id}/restore")
async def restore_agent(
    agent_id: int,
    current_user: User = Depends(require_perm("agent_manage.delete.restore")),
    db: Session = Depends(get_db)
):
    """恢复Agent（仅管理员）"""
    agent_service = AgentService(db)
    agent_service.restore_agent(agent_id)
    agent_manager.clear_cache()
    cache_delete_pattern("agents:*")
    return {"message": "Agent已恢复"}

@router.delete("/{agent_id}/hard")
async def hard_delete_agent(
    agent_id: int,
    data: DestroyRequest,
    current_user: User = Depends(require_perm("agent_manage.delete.hard")),
    db: Session = Depends(get_db)
):
    """彻底删除Agent（硬删除，需要重新输入管理员密码）"""
    # 验证管理员密码
    # 凭据按账号查找，兼容旧用户名
    admin_user = db.query(UserModel).filter(
        (UserModel.account == data.username) | (UserModel.username == data.username)
    ).first()
    if not admin_user or admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号不是管理员")
    if not verify_password(data.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="密码错误")
    
    agent_service = AgentService(db)
    try:
        agent_service.hard_delete_agent(agent_id)
        agent_manager.clear_cache()
        cache_delete_pattern("agents:*")
        return {"message": "Agent已彻底删除，数据不可恢复"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
