from sqlalchemy.orm import Session
from typing import List
import json
from datetime import datetime
from app.models.session import Session as SessionModel
from app.models.message import Message
from app.models.agent import Agent
from app.models.file import FileRecord


'''对话的管理：创建会话、查会话列表、收发消息、删除会话'''


class SessionService:
    def __init__(self, db: Session):
        self.db = db
    
    def create_session(self, user_id: int, agent_id: int, title: str = "新对话") -> SessionModel:
        """创建新会话"""
        # user_id 由第 6 层路由从 JWT 里取出来传入，不让前端传，防伪造
        session = SessionModel(
            user_id=user_id,
            agent_id=agent_id,
            title=title
        )
        from app.config import settings
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from fastapi import HTTPException
            from app.models.agent_governance import AgentPublication, SessionAgentVersion
            published = self.db.get(AgentPublication, agent_id, with_for_update=True)
            if published is None:
                raise HTTPException(503, "Agent 未建立已发布基线，不能创建会话")
        self.db.add(session)
        self.db.flush()
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            self.db.add(SessionAgentVersion(session_id=session.id, agent_id=agent_id,
                                            version_id=published.version_id))
        self.db.commit()
        self.db.refresh(session)
        return session
    
    def get_session_by_id(self, session_id: int) -> SessionModel:
        """根据ID获取会话"""
        session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
        if not session:
            raise ValueError("会话不存在")
        return session
    
    def get_user_sessions(self, user_id: int, agent_id: int = None) -> List[SessionModel]:
        """获取用户的所有会话"""
        query = self.db.query(SessionModel).filter(SessionModel.user_id == user_id)
        if agent_id is not None:
            query = query.filter(SessionModel.agent_id == agent_id)
        return query.order_by(SessionModel.updated_at.desc()).all()
    
    def get_session_messages(self, session_id: int, limit: int = 50, before_id: int = None) -> tuple:
        """获取会话的消息历史（分页加载，最新50条优先）
        
        Args:
            session_id: 会话ID
            limit: 每次加载多少条（默认50）
            before_id: 加载这个ID之前的消息（None=加载最新的）
        
        Returns:
            (messages, has_more) — 消息列表 + 是否还有更早的消息
        """
        query = self.db.query(Message).filter(Message.session_id == session_id)

        if before_id is not None:
            # 加载指定ID之前的消息
            query = query.filter(Message.id < before_id)

        # 多取 1 条来判断是否还有更早的消息（避免总数恰好等于 limit 时误判 has_more）
        # 按自增主键 id 排序：与时间序一致，且不受 created_at 相同导致的排序不稳定影响
        fetched = query.order_by(Message.id.desc()).limit(limit + 1).all()
        has_more = len(fetched) > limit
        messages = fetched[:limit]

        # 翻转为正序（时间从早到晚，聊天界面从上到下）
        messages.reverse()

        return (messages, has_more)

    def get_session_usage(self, session_id: int) -> dict | None:
        '''Aggregate all stored replies, independently of message pagination.'''
        runs = {}
        last_run_id = None
        rows = (
            self.db.query(Message.id, Message.metadata_json)
            .filter(
                Message.session_id == session_id,
                Message.role == 'agent',
                Message.metadata_json.like('%"token_usage"%'),
            )
            .order_by(Message.id)
            .yield_per(200)
        )
        for message_id, raw in rows:
            try:
                metadata = json.loads(raw)
                usage = metadata.get('token_usage')
                if not isinstance(usage, dict):
                    continue
                input_tokens = int(usage['input_tokens'])
                output_tokens = int(usage['output_tokens'])
                if input_tokens < 0 or output_tokens < 0:
                    continue
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            run_id = metadata.get('run_id') or f'message:{message_id}'
            previous = runs.get(run_id)
            if previous is None or input_tokens + output_tokens >= previous[0] + previous[1]:
                cached = usage.get('cache_input_tokens')
                try:
                    cached = int(cached) if cached is not None else None
                except (TypeError, ValueError):
                    cached = None
                runs[run_id] = (input_tokens, output_tokens, cached)
            last_run_id = metadata.get('run_id') or last_run_id
        if not runs:
            return None
        total_input = sum(item[0] for item in runs.values())
        total_output = sum(item[1] for item in runs.values())
        cache_known = all(item[2] is not None for item in runs.values())
        total_cache = sum(item[2] or 0 for item in runs.values()) if cache_known else None
        return {
            'input_tokens': total_input,
            'output_tokens': total_output,
            'cache_input_tokens': total_cache,
            'cache_rate': round(total_cache / total_input * 100, 1) if cache_known and total_input else (0 if cache_known else None),
            'last_run_id': last_run_id,
        }
    
    def add_message(self, session_id: int, role: str, content: str, agent_name: str = None, metadata: dict = None) -> Message:
        """添加一条消息

        metadata: 附加信息，序列化为 JSON 存入 metadata_json
          - agent 消息: {"steps":[...], "file_outputs":[...], "token_usage":{...}, "tool_call_count":N}
          - user 消息:  {"attachments":[{"name":..., "stored_name":...}]}
        """
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            agent_name=agent_name,
            metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None
        )
        self.db.add(message)
        # 同步刷新会话活跃时间：会话列表按 updated_at 倒序展示，
        # 此前收发消息不更新该字段，活跃会话不会上浮、排序失真
        self.db.query(SessionModel).filter(SessionModel.id == session_id).update(
            {"updated_at": datetime.now()}, synchronize_session=False
        )
        self.db.commit()
        self.db.refresh(message)
        return message
    
    def delete_session(self, session_id: int, user_id: int):
        """删除会话（只能删除自己的）"""
        # 双重过滤：session_id 和 user_id 同时匹配才允许删除。用户只能删自己的会话，不能通过猜别人的 session_id 来删别人的
        # 查不到就抛异常（可能是 ID 不存在，也可能是存在但不是你的）。
        session = self.db.query(SessionModel).filter(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id
        ).first()

        if not session:
            raise ValueError("会话不存在或无权删除")

        # 文件中心：保留产出文件的 session_id 作为"来源会话已删除"的判定依据，
        # 并把标题写进快照，删除后文件中心仍能展示原会话标题（置灰不可点）而不是整列消失
        self.db.query(FileRecord).filter(
            FileRecord.session_id == session_id,
            FileRecord.session_title.is_(None),
        ).update({"session_title": session.title}, synchronize_session=False)

        # PostgreSQL FK 约束（files.session_id -> sessions.id）：删除会话前必须置空引用，
        # 否则 FK 会阻止 DELETE。session_title 快照已保存，文件中心仍能展示来源标题。
        # SQLite 开发环境不强制 FK，此步在两种数据库下都安全（幂等）。
        self.db.query(FileRecord).filter(
            FileRecord.session_id == session_id,
        ).update({"session_id": None}, synchronize_session=False)

        # 物理删除，因为 Session 模型定义了 cascade="all, delete-orphan" 在 messages 关系上，所以删会话时数据库会自动级联删除该会话下的所有消息。
        self.db.delete(session)
        self.db.commit()

    def update_session_title(self, session_id: int, user_id: int, title: str):
        """更新会话标题"""
        session = self.db.query(SessionModel).filter(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id
        ).first()
        
        if not session:
            raise ValueError("会话不存在或无权修改")
        
        session.title = title
        # 同步刷新文件中心的标题快照，避免会话改名后"来源"显示旧标题
        self.db.query(FileRecord).filter(
            FileRecord.session_id == session_id
        ).update({"session_title": title}, synchronize_session=False)
        self.db.commit()
        self.db.refresh(session)
        return session

    def branch_session(self, session_id: int, user_id: int, from_message_id: int) -> SessionModel:
        """分支：复制会话到 from_message_id 为止的全部消息，新标题 = (分支)+原标题"""
        session = self.db.query(SessionModel).filter(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id,
        ).first()
        if not session:
            raise ValueError("会话不存在或无权操作")
        new_session = SessionModel(
            user_id=user_id,
            agent_id=session.agent_id,
            title=f"(分支){session.title}",
        )
        from app.config import settings
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from fastapi import HTTPException
            from app.models.agent_governance import SessionAgentVersion
            source_pin = self.db.get(SessionAgentVersion, session.id)
            if source_pin is None or source_pin.agent_id != session.agent_id:
                raise HTTPException(409, '原会话未固定 Agent 版本，不能创建分支')
        self.db.add(new_session)
        self.db.flush()
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            self.db.add(SessionAgentVersion(session_id=new_session.id, agent_id=session.agent_id,
                                            version_id=source_pin.version_id))
        messages = self.db.query(Message).filter(
            Message.session_id == session_id,
            Message.id <= from_message_id,
        ).order_by(Message.id.asc()).all()
        for msg in messages:
            self.db.add(Message(
                session_id=new_session.id,
                role=msg.role,
                content=msg.content,
                agent_name=msg.agent_name,
                metadata_json=msg.metadata_json,
            ))
        self.db.commit()
        self.db.refresh(new_session)
        return new_session

    def delete_message_pair(self, session_id: int, message_id: int, user_id: int) -> dict:
        """撤销用户消息：删除该 user 消息及其后的 agent 回复，返回被删内容供前端回填"""
        session = self.db.query(SessionModel).filter(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id,
        ).first()
        if not session:
            raise ValueError("会话不存在或无权操作")
        user_msg = self.db.query(Message).filter(
            Message.session_id == session_id,
            Message.id == message_id,
            Message.role == "user",
        ).first()
        if not user_msg:
            raise ValueError("消息不存在或不是用户消息")
        attachments = []
        if user_msg.metadata_json:
            try:
                meta = json.loads(user_msg.metadata_json)
                attachments = meta.get("attachments", [])
            except (json.JSONDecodeError, TypeError):
                pass
        agent_reply = self.db.query(Message).filter(
            Message.session_id == session_id,
            Message.id > message_id,
            Message.role == "agent",
        ).order_by(Message.id.asc()).first()
        if agent_reply:
            self.db.delete(agent_reply)
        content = user_msg.content
        self.db.delete(user_msg)
        self.db.commit()
        return {"content": content, "attachments": attachments}
