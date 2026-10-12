"""Resolve memory behavior from the conversation's effective Agent version."""

from enum import Enum

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent import Agent
from app.models.agent_governance import AgentVersion, SessionAgentVersion
from app.models.session import Session as Conversation


class MemoryPolicy(str, Enum):
    READ_ONLY = "stage6_disabled"
    DISABLED = "disabled"
    CANDIDATE_REVIEW = "candidate_review"
    LEGACY_AUTO = "legacy_auto"

    @property
    def allows_extraction(self) -> bool:
        return self in (self.CANDIDATE_REVIEW, self.LEGACY_AUTO)


def session_memory_policy(db: Session, user_id: int, session_id: str) -> MemoryPolicy:
    key = str(session_id)
    conversation = db.get(Conversation, int(key)) if key.isdigit() else None
    if conversation is None:
        raise HTTPException(409, "记忆来源会话不存在")
    if conversation.user_id != user_id:
        raise HTTPException(403, "来源会话不属于记忆属主")
    if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        pin = db.get(SessionAgentVersion, conversation.id)
        if pin is None or pin.agent_id != conversation.agent_id:
            raise HTTPException(409, "会话记忆策略缺少有效的 Agent 基线")
        version = db.get(AgentVersion, pin.version_id)
        if version is None or version.agent_id != conversation.agent_id or version.state != "published":
            raise HTTPException(409, "会话记忆策略的 Agent 版本已失效")
        value = (version.snapshot or {}).get("memory_policy", MemoryPolicy.READ_ONLY.value)
        allowed = (MemoryPolicy.READ_ONLY, MemoryPolicy.DISABLED, MemoryPolicy.CANDIDATE_REVIEW)
    else:
        agent = db.get(Agent, conversation.agent_id)
        if agent is None:
            raise HTTPException(409, "记忆来源 Agent 不存在")
        config = agent.config if isinstance(agent.config, dict) else {}
        value = config.get("_memory_policy", config.get("memory_policy", MemoryPolicy.LEGACY_AUTO.value))
        allowed = tuple(MemoryPolicy)
    try:
        policy = MemoryPolicy(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "会话长期记忆策略无效，已停止自动提取") from exc
    if policy not in allowed:
        raise HTTPException(409, "会话长期记忆策略无效，已停止自动提取")
    return policy
