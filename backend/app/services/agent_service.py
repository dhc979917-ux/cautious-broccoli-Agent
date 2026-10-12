from sqlalchemy.orm import Session
from typing import List
from app.models.agent import Agent
from app.models.session import Session as SessionModel
from app.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.config import settings
from fastapi import HTTPException
from sqlalchemy import text


'''AI 助手的管理：查列表、查详情、创建 Agent、软删除 Agent'''

class AgentService:
    def __init__(self, db: Session):
        self.db = db
    
    def get_all_agents(self) -> List[Agent]:
        """获取所有活跃的Agent列表"""
        #  只查活跃的 Agent，软删除的（is_active=False）自动过滤掉
        return self.db.query(Agent).filter(Agent.is_active == True).all()
    
    def get_agent_by_id(self, agent_id: int) -> Agent:
        """根据ID获取Agent"""
        #  按 ID 查，查不到抛异常。注意：不过滤 is_active，所以软删除的 Agent 也能查到（给内部逻辑用的）
        agent = self.db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            raise ValueError("Agent不存在")
        return agent
    
    def create_agent(self, data: CreateAgentRequest) -> Agent:
        """创建新Agent（管理员功能）"""
        # 从 Schema 取字段 → 构造 ORM 对象 → 写库 → refresh 让 ORM 对象同步数据库生成的 id 和 created_at → 返回
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            raise HTTPException(409, "旧 Agent 创建入口已禁用，请改用后台草稿创建接口")
        agent = Agent(
            name=data.name,
            description=data.description,
            agent_type=data.agent_type,
            model_config_name=self._resolve_model(data),
            system_prompt=data.system_prompt,
            config=data.config
        )
        self.db.add(agent)
        self.db.commit()
        self.db.refresh(agent)
        return agent

    # 基底 Agent：两种 agent_type 各自对应的预置 Agent（前端 BASE_AGENT_NAMES 同名约定）。
    # 新建 Agent 未显式指定模型时，继承对应基底 Agent 的模型，避免所有新 Agent 都落同一个全局默认值
    BASE_AGENT_NAME_BY_TYPE = {"dialog": "工具助手", "task": "数据分析师"}

    def _resolve_model(self, data: CreateAgentRequest) -> str:
        """模型解析：显式指定 > 继承同类型基底 Agent > 同类型最早的 Agent > 全局默认"""
        if data.model_config_name:
            return data.model_config_name
        base = None
        base_name = self.BASE_AGENT_NAME_BY_TYPE.get(data.agent_type)
        if base_name:
            base = self.db.query(Agent).filter(Agent.name == base_name).first()
        if not base:
            base = self.db.query(Agent).filter(Agent.agent_type == data.agent_type)\
                .order_by(Agent.id.asc()).first()
        if base and base.model_config_name:
            return base.model_config_name
        return settings.MODEL_NAME
    
    def delete_agent(self, agent_id: int):
        """删除Agent（软删除，标记为inactive）"""
        # 软删除：不改数据库记录，只把 is_active 改成 False。保留历史数据（会话记录里的 agent_id 不会变成悬挂外键）
        agent = self.get_agent_by_id(agent_id)
        agent.is_active = False
        self.db.commit()

    def update_agent(self, agent_id: int, data: UpdateAgentRequest) -> Agent:
        """更新Agent（仅名称/描述/系统提示词，管理员功能）"""
        # 只允许修改这三个字段；agent_type/model/config 等其余字段一律不动
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            raise HTTPException(409, "Agent 已启用版本治理，请在后台创建草稿并发布")
        agent = self.get_agent_by_id(agent_id)
        if data.name is not None:
            agent.name = data.name
        if data.description is not None:
            agent.description = data.description
        if data.system_prompt is not None:
            agent.system_prompt = data.system_prompt
        if data.model_config_name is not None:
            agent.model_config_name = data.model_config_name
        if data.config is not None:
            # 增量合并：只更新白名单键，保留 builtin_skills 等既有配置
            merged = dict(agent.config or {})
            for key in ("mcp_servers", "knowledge_bases"):
                if key in data.config:
                    merged[key] = data.config[key]
            if "max_react_iters" in data.config:
                if data.config["max_react_iters"]:
                    merged["max_react_iters"] = int(data.config["max_react_iters"])
                else:
                    merged.pop("max_react_iters", None)
            agent.config = merged
        self.db.commit()
        self.db.refresh(agent)
        return agent

    def get_all_agents_include_deleted(self) -> List[Agent]:
        """获取所有Agent（包括已删除的，管理员用）"""
        return self.db.query(Agent).order_by(Agent.id.desc()).all()

    def restore_agent(self, agent_id: int):
        """恢复Agent（把is_active改回True）"""
        agent = self.get_agent_by_id(agent_id)
        agent.is_active = True
        self.db.commit()

    def hard_delete_agent(self, agent_id: int):
        """Hard-delete an agent (irreversible). Delete every referencing row in FK-safe order via raw SQL, then the agent."""
        agent = self.get_agent_by_id(agent_id)  # raises ValueError -> 404 if missing
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from app.models.agent_governance import AgentVersion
            from app.models.user_agent_access import UserAgentAccess
            from app.models.tool_governance import AgentToolGrant
            if (self.db.query(SessionModel.id).filter_by(agent_id=agent_id).first() or
                self.db.query(UserAgentAccess.id).filter_by(agent_id=agent_id).first() or
                self.db.query(AgentToolGrant.id).filter_by(agent_id=agent_id).first() or
                self.db.execute(text("SELECT id FROM scheduled_jobs WHERE agent_id = :agent_id LIMIT 1"),
                                {"agent_id": agent_id}).first()):
                raise HTTPException(409, "Agent 仍有关联会话、用户授权或计划任务，请先处理关联数据后再彻底删除。")
            from app.models.agent_governance import AgentPublication, AgentReleaseEvent, SessionAgentVersion
            if self.db.query(SessionAgentVersion.session_id).filter_by(agent_id=agent_id).first():
                raise HTTPException(409, 'Agent 仍被历史会话引用，不能彻底删除。')
            self.db.query(AgentReleaseEvent).filter_by(agent_id=agent_id).delete(synchronize_session=False)
            self.db.query(AgentPublication).filter_by(agent_id=agent_id).delete(synchronize_session=False)
            self.db.query(AgentVersion).filter_by(agent_id=agent_id).delete(synchronize_session=False)
        aid = agent_id
        text_sql = text

        # ids of sessions / jobs owned by this agent
        sids = [r[0] for r in self.db.execute(text_sql("SELECT id FROM sessions WHERE agent_id = :a"), {"a": aid}).fetchall()]
        jids = [r[0] for r in self.db.execute(text_sql("SELECT id FROM scheduled_jobs WHERE agent_id = :a"), {"a": aid}).fetchall()]

        # 1) messages on those sessions (messages.session_id -> sessions)
        if sids:
            self.db.execute(text_sql("DELETE FROM messages WHERE session_id = ANY(:s)"), {"s": sids})
        # 2) files referencing the agent or its sessions
        self.db.execute(text_sql("DELETE FROM files WHERE agent_id = :a"), {"a": aid})
        if sids:
            self.db.execute(text_sql("DELETE FROM files WHERE session_id = ANY(:s)"), {"s": sids})
        # 3) scheduled job runs + jobs (runs.job_id -> jobs)
        if jids:
            self.db.execute(text_sql("DELETE FROM scheduled_job_runs WHERE job_id = ANY(:j)"), {"j": jids})
        self.db.execute(text_sql("DELETE FROM scheduled_jobs WHERE agent_id = :a"), {"a": aid})
        # 4) user-agent access grants
        self.db.execute(text_sql("DELETE FROM user_agent_access WHERE agent_id = :a"), {"a": aid})
        # 5) the sessions themselves
        self.db.execute(text_sql("DELETE FROM sessions WHERE agent_id = :a"), {"a": aid})
        # 6) finally the agent
        self.db.execute(text_sql("DELETE FROM agents WHERE id = :a"), {"a": aid})
        self.db.commit()
