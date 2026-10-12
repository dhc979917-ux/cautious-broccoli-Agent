"""Agent execution access, separate from read-only discovery grants."""

from sqlalchemy.orm import Session

from app.models.user_agent_access import UserAgentAccess


def can_execute_agent(db: Session, user_id: int, agent_id: int, role: str) -> bool:
    if role == "admin":
        return True
    return db.query(UserAgentAccess.id).filter(
        UserAgentAccess.user_id == user_id,
        UserAgentAccess.agent_id == agent_id,
        UserAgentAccess.access_level.in_(("execute", "admin")),
    ).first() is not None
