"""Add reviewed MCP Tool catalog, Agent grants, and one-use approvals."""

import argparse

from sqlalchemy import inspect

from app.database import engine
from app.models.tool_governance import AgentToolGrant, McpToolCatalog, ToolApproval


TABLES = (McpToolCatalog.__table__, AgentToolGrant.__table__, ToolApproval.__table__)


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    present = set(inspect(target).get_table_names())
    if not {"users", "agents", "mcp_servers", "sessions", "audit_events"}.issubset(present):
        raise RuntimeError("Existing Agent, identity, session, MCP, and audit tables are required")
    missing = [table.name for table in TABLES if table.name not in present]
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print("Stage-3 schema: " + (", ".join(missing) if missing else "up to date"))
    return missing


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(parser.parse_args().apply)
