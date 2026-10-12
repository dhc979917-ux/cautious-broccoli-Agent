"""SQLite-only MCP UI fixture with no production mounts or external tool execution."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets

from app.config import settings
from app.database import SessionLocal, init_db


def create_app():
    if os.environ.get('MCP_GOVERNANCE_ISOLATED') != '1' or not settings.DATABASE_URL.startswith('sqlite:'):
        raise RuntimeError('Explicit isolated SQLite configuration required')
    from app.main import app
    from app.core.security import create_access_token, get_password_hash
    from app.models.user import User
    from app.models.user_agent_access import UserAgentAccess
    from app.models.agent import Agent
    from app.models.mcp_server import McpServer
    from app.models.tool_governance import McpToolCatalog
    from app.models.model_governance import GatewayModelEntry
    from app.services import model_gateway, model_access, model_governance, agent_versions
    from app.services.mcp_descriptions import enqueue_descriptions
    from app.api.v1 import models
    init_db()
    directory = {'models': [{'model_id': 'e-fixture', 'provider': 'fixture'}],
        'fetched_at': datetime.now(timezone.utc).isoformat(), 'stale': False, 'gateway_reachable': True}
    model_gateway.chat_models = lambda force=False: ['e-fixture']
    model_gateway.gateway_directory = lambda force=False: dict(directory)
    model_access.chat_models = model_gateway.chat_models
    model_governance.chat_models = model_gateway.chat_models
    model_governance.gateway_directory = model_gateway.gateway_directory
    models.chat_models = model_gateway.chat_models
    identities = {}
    with SessionLocal() as db:
        db.add(GatewayModelEntry(model_id='e-fixture', seen_at=datetime.now(timezone.utc)))
        agent = Agent(name='MCP 隔离验收助手', agent_type='dialog', model_config_name='e-fixture',
                      config={'mcp_servers': list(range(1, 11))}, is_active=True)
        db.add(agent); db.flush()
        for role in ['admin', 'viewer', 'operator']:
            user = User(username={'admin': '验收管理员', 'viewer': '验收观察者', 'operator': '验收操作员'}[role],
                        account='qa_mcp52_' + role, role=role, password_hash=get_password_hash(secrets.token_urlsafe(24)),
                        is_active=True, permissions='["chat.use"]')
            db.add(user); db.flush()
            db.add(UserAgentAccess(user_id=user.id, agent_id=agent.id, access_level='execute'))
            identities[role] = {'id': user.id, 'token': create_access_token({'sub': str(user.id), 'tv': user.token_version,
                                'username': user.username, 'role': role, 'agent_permissions': [str(agent.id)]}),
                                'user': {'id': user.id, 'username': user.username, 'account': user.account, 'role': role,
                                         'is_active': True, 'permissions': [], 'agent_permissions': [str(agent.id)]}}
        for index in range(1, 11):
            server = McpServer(id=index, name='web' if index == 1 else f'MCP-{index:02}', type='stdio', command='python3',
                args_json='["/app/app/agents/mcp_servers/web_mcp_server.py"]',
                enabled=index != 10, description='读取网页并提取正文，支持获取页面中的链接。',
                display_description='提供网页正文读取及链接整理能力，帮助助手阅读在线文章、检索参考资料，并了解网页内容和关联资源。',
                source_files={'web_fetch': {'path': '/app/app/agents/mcp_servers/web_mcp_server.py'}})
            db.add(server)
            db.add(McpToolCatalog(server_id=index, name='web_fetch', description='Original raw documentation',
                display_description='获取指定网页并提取正文信息，便于助手阅读在线文章、查询文档资料，并理解页面中的主要内容。', description_status='ready',
                schema_digest='a' * 64, input_schema={'type': 'object', 'properties': {'url': {'type': 'string'}}},
                risk='low', revision=1))
            db.add(McpToolCatalog(server_id=index, name='send_email', description='Send mail',
                display_description='向指定收件人发送新邮件，支持填写主题与正文内容，适用于主动通知、信息传递以及日常邮件沟通。', description_status='ready',
                schema_digest='b' * 64, input_schema={}, risk='high', revision=1))
        db.commit(); agent_versions.baseline(db, agent.id)
    Path('/tmp/mcpgovernance52-identity.json').write_text(json.dumps(identities, ensure_ascii=False), encoding='utf-8')
    @asynccontextmanager
    async def lifespan(application):
        yield
    app.router.lifespan_context = lifespan
    return app
