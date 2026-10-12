"""Add immutable Agent versions, publication history, and session pins."""

import argparse

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.database import engine
from app.models.agent import Agent
from app.models.agent_governance import AgentPublication, AgentReleaseEvent, AgentVersion, SessionAgentVersion
from app.models.session import Session as ChatSession
from app.services.agent_versions import baseline


TABLES = (AgentVersion.__table__, AgentPublication.__table__,
          AgentReleaseEvent.__table__, SessionAgentVersion.__table__)


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if not {"users", "departments", "agents", "sessions", "skill_versions", "mcp_tool_catalog"}.issubset(existing):
        raise RuntimeError("Stage-1, -3 and -4 schemas are required for Agent versioning")
    missing = [table.name for table in TABLES if table.name not in existing]
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print("Stage-5 schema: " + (", ".join(missing) if missing else "up to date"))
    return missing


def backfill(database_engine=None) -> list[int]:
    target = database_engine if database_engine is not None else engine
    published = []
    with Session(target) as db:
        for (agent_id,) in db.query(Agent.id).order_by(Agent.id):
            if db.get(AgentPublication, agent_id) is None:
                baseline(db, agent_id)
                published.append(agent_id)
    return published


def backfill_sessions(database_engine=None) -> int:
    target = database_engine if database_engine is not None else engine
    pinned = 0
    last_id = 0
    with Session(target) as db:
        while True:
            rows = db.query(ChatSession.id, ChatSession.agent_id, AgentPublication.version_id).join(
                AgentPublication, AgentPublication.agent_id == ChatSession.agent_id
            ).outerjoin(SessionAgentVersion, SessionAgentVersion.session_id == ChatSession.id).filter(
                ChatSession.id > last_id, SessionAgentVersion.session_id.is_(None)
            ).order_by(ChatSession.id).limit(500).all()
            if not rows:
                break
            db.add_all(SessionAgentVersion(session_id=session_id, agent_id=agent_id,
                                            version_id=version_id)
                       for session_id, agent_id, version_id in rows)
            db.commit()
            pinned += len(rows)
            last_id = rows[-1][0]
    return pinned


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    arguments = parser.parse_args()
    migrate(arguments.apply)
    if arguments.baseline:
        if not arguments.apply:
            parser.error("--baseline requires --apply")
        print("Stage-5 published baselines:", backfill())
        print('Stage-5 historical session pins:', backfill_sessions())
