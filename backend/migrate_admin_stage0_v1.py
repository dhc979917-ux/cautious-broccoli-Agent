"""Add stage-0 audit storage and MCP credential rotation metadata.

Run without arguments to check only; run --apply after a verified database backup.
"""

import argparse

from sqlalchemy import inspect, text

from app.database import Base, engine
from app.models.audit_event import AuditEvent


def migrate(apply: bool = False) -> list[str]:
    inspector = inspect(engine)
    pending = []
    if "audit_events" not in inspector.get_table_names():
        pending.append("audit_events")
    if "env_rotated_at" not in {column["name"] for column in inspector.get_columns("mcp_servers")}:
        pending.append("mcp_servers.env_rotated_at")
    if apply:
        if "audit_events" in pending:
            Base.metadata.create_all(engine, tables=[AuditEvent.__table__], checkfirst=True)
        if "mcp_servers.env_rotated_at" in pending:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN env_rotated_at TIMESTAMP"))
    print("Stage-0 schema: " + (", ".join(pending) if pending else "up to date"))
    return pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(parser.parse_args().apply)
