import argparse
import asyncio

from app.config import settings
from app.database import SessionLocal
from app.models.agent import Agent
from app.models.mcp_server import McpServer
from app.models.tool_governance import AgentToolGrant, McpToolCatalog
from app.services.tool_governance import refresh_catalog


async def migrate(apply=False):
    if settings.GOVERNANCE_TOOL_POLICY_ENABLED or settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        raise RuntimeError('Baseline must run before switching the versioned runtime')
    with SessionLocal() as db:
        servers = db.query(McpServer).filter_by(enabled=True).all()
        agents = db.query(Agent).all()
        if not apply:
            print('Enabled MCP servers:', len(servers), 'agents:', len(agents))
            return
        for server in servers:
            await refresh_catalog(db, server.id)
            for tool in db.query(McpToolCatalog).filter_by(server_id=server.id, available=True):
                if tool.risk == 'unknown':
                    tool.risk = 'high'
                    tool.revision += 1
            db.commit()
        for agent in agents:
            bindings = (agent.config or {}).get('mcp_servers') or []
            if not isinstance(bindings, list):
                raise RuntimeError('Invalid existing MCP bindings')
            for tool in db.query(McpToolCatalog).filter(McpToolCatalog.server_id.in_(bindings), McpToolCatalog.available.is_(True)):
                existing = db.query(AgentToolGrant).filter_by(agent_id=agent.id, server_id=tool.server_id, tool_name=tool.name).first()
                if existing and existing.schema_digest != tool.schema_digest:
                    raise RuntimeError('Existing grant schema changed; manual review required')
                if existing is None:
                    db.add(AgentToolGrant(agent_id=agent.id, server_id=tool.server_id, tool_name=tool.name, schema_digest=tool.schema_digest))
            db.commit()
        print('Reviewed catalog entries:', db.query(McpToolCatalog).count(), 'preserved binding grants:', db.query(AgentToolGrant).count())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    asyncio.run(migrate(parser.parse_args().apply))
