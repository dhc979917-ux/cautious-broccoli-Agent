"""Exercise the real draft builder and toolkit using synthetic dependency adapters."""

import asyncio
import json
import sys


async def run_isolated_agent(agent_id: int, version_id: str, revision: int) -> bool:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.services.agent_draft_probe", str(agent_id),
        version_id, str(revision), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=35)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return False
    try:
        return process.returncode == 0 and json.loads(stdout.splitlines()[-1])["ok"] is True
    except (ValueError, KeyError, IndexError, TypeError):
        return False


def _probe(agent_id: int, version_id: str, revision: int) -> bool:
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch

    from agentscope.message import Msg, TextBlock
    from agentscope.model import ChatResponse

    from app.agents.architectures import get_builder
    from app.agents.architectures import base
    from app.agents import model_factory, workspace
    from app.config import settings
    from app.database import SessionLocal
    from app.models.agent_governance import AgentVersion

    class SyntheticModel:
        model = "synthetic-only"
        context_size = 131072

        def __init__(self, calls):
            self.calls = calls

        async def __call__(self, *args, **kwargs):
            return ChatResponse(content=[TextBlock(text="synthetic response")], is_last=True)

        async def count_tokens(self, *args, **kwargs):
            return 5

    class TemporaryWorkspace:
        def __init__(self, root):
            self.workdir = str(root)

        async def initialize(self):
            return None

    with SessionLocal() as db, tempfile.TemporaryDirectory() as directory:
        version = db.get(AgentVersion, version_id)
        if version is None or version.agent_id != agent_id or version.revision != revision:
            return False
        snapshot = version.snapshot
        config = snapshot["config"].copy()
        from agentscope.tool._adapters import MCPTool
        from agentscope.tool import ToolBase, ToolChunk
        from mcp.types import Tool, CallToolResult, TextContent
        from app.models.mcp_server import McpServer
        from app.models.tool_governance import McpToolCatalog
        from app.services.mcp_service import McpService
        from app.agents import tool_policy
        from app.services.knowledge_service import KnowledgeService
        from app.agents.tools import search_knowledge, write_note, trace_knowledge
        from app.services.skill_versions import runtime_dirs

        visited = set()
        calls = []

        def sample(schema):
            if schema.get('enum'):
                return schema['enum'][0]
            kind = schema.get('type', 'object')
            if kind == 'object':
                return {key: sample(schema.get('properties', {}).get(key, {}))
                        for key in schema.get('required', [])}
            if kind == 'array':
                return [sample(schema.get('items', {'type': 'string'}))]
            return {'string': 'synthetic', 'integer': 1, 'number': 1.0,
                    'boolean': False, 'null': None}.get(kind, 'synthetic')

        class MockSession:
            def __init__(self, server_id):
                self.server_id = server_id

            async def call_tool(self, name, arguments, **kwargs):
                visited.add(f'tool:{self.server_id}:{name}')
                return CallToolResult(content=[TextContent(type='text', text='synthetic dependency response')])

        class MockClient:
            def __init__(self, record, tools):
                self.name = record.name
                self.tools = tools
                self.session = MockSession(record.id)
                self.is_stateful = False

            async def list_raw_tools(self):
                return self.tools

            async def get_tool(self, name):
                tool = next(tool for tool in self.tools if tool.name == name)
                return MCPTool(mcp_name=self.name, tool=tool, session=self.session)

            async def list_tools(self):
                return [await self.get_tool(tool.name) for tool in self.tools]

            async def close(self, **kwargs):
                return None

        clients = []
        for server_id in config.get('mcp_servers', []):
            record = db.get(McpServer, server_id)
            granted = [item for item in snapshot['tool_grants'] if item['server_id'] == server_id]
            tools = []
            for grant in granted:
                catalog = db.query(McpToolCatalog).filter_by(server_id=server_id, name=grant['tool_name']).one()
                tools.append(Tool(name=catalog.name, description=catalog.description,
                                  inputSchema=catalog.input_schema, outputSchema=catalog.output_schema))
                calls.append((tool_policy.registration_name(record.name, catalog.name), sample(catalog.input_schema)))
            if not tools:
                return False
            clients.append(MockClient(record, tools))

        async def mock_clients(*args, **kwargs):
            return clients

        def scoped_clients(db, wrapped, server_ids, agent_id, session_id):
            return [tool_policy.VisibleMCPClient(client, {
                item['tool_name']: item['schema_digest'] for item in snapshot['tool_grants']
                if db.get(McpServer, item['server_id']).name == client.name
            }) for client in wrapped]

        def mock_knowledge(name, schema):
            class KnowledgeTool(ToolBase):
                description = 'Synthetic knowledge dependency used only by the isolated draft probe'
                input_schema = schema
                is_read_only = True
                is_concurrency_safe = True

                def __init__(self, *args, **kwargs):
                    super().__init__()
                    self.name = name

                async def call(self, **kwargs):
                    visited.add('knowledge:' + name)
                    return ToolChunk(content=[TextBlock(text='synthetic knowledge response')])

                async def check_permissions(self, tool_input, context):
                    from agentscope.permission import PermissionBehavior, PermissionDecision
                    return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='synthetic only')
            return KnowledgeTool

        selected_kbs = config.get('knowledge_bases', [])
        if selected_kbs:
            for tool_class in (search_knowledge.SearchKnowledge, write_note.WriteNote, trace_knowledge.TraceKnowledge):
                calls.append((tool_class.name, sample(tool_class.input_schema)))
        optional, builtin = runtime_dirs(db, SimpleNamespace(config={**config, '_agent_version_id': version_id}),
                                          [], 'viewer', get_builder(snapshot['agent_type'])._builtin_skills_root())
        for directory_path in builtin + [item[0] for item in optional]:
            if not (directory_path / 'SKILL.md').read_text(encoding='utf-8').strip():
                return False
        config['max_react_iters'] = max(5, len(calls) + 2)
        model = SimpleNamespace(
            id=agent_id, name=snapshot["name"], agent_type=snapshot["agent_type"],
            model_config_name=snapshot["model_config_name"],
            system_prompt=snapshot.get("system_prompt"),
            config={**config, "_agent_version_id": version_id, "_tool_grants": snapshot['tool_grants']},
        )
        root = Path(directory)
        with patch.object(settings, "WORKSPACE_BACKEND", "local"), patch.object(
            settings, "GOVERNANCE_RESOURCE_VERSIONING_ENABLED", True
        ), patch.object(base, "OUTPUT_DIR", root), patch.object(
            workspace, "get_workspace", return_value=TemporaryWorkspace(root)
        ), patch.object(model_factory, "get_model", return_value=SyntheticModel(calls)), patch.object(
            settings, 'GOVERNANCE_TOOL_POLICY_ENABLED', False
        ), patch.object(
            McpService, 'build_clients', new=mock_clients
        ), patch.object(tool_policy, 'visible_clients', new=scoped_clients), patch.object(
            tool_policy, 'protect_toolkit', return_value=None
        ), patch.object(tool_policy, 'protect_access', return_value=None), patch(
            'app.services.mcp_access.allowed_ids', return_value=config.get('mcp_servers', [])
        ), patch.object(tool_policy, 'risk_for', return_value='low'), patch.object(
            search_knowledge, 'SearchKnowledge', mock_knowledge('SearchKnowledge', search_knowledge.SearchKnowledge.input_schema)
        ), patch.object(base, 'SearchKnowledge', mock_knowledge('SearchKnowledge', search_knowledge.SearchKnowledge.input_schema)), patch.object(
            write_note, 'WriteNote', mock_knowledge('WriteNote', write_note.WriteNote.input_schema)
        ), patch.object(trace_knowledge, 'TraceKnowledge', mock_knowledge('TraceKnowledge', trace_knowledge.TraceKnowledge.input_schema)), patch.object(
            settings, 'MCP_REQUIRE_CONFIRM_TOOLS', ''
        ):
            agent = asyncio.run(get_builder(snapshot["agent_type"]).build(
                model, selected_skills=[], user_id=None, db=db))
            agent.state.tool_context.activated_groups = [group.name for group in agent.toolkit.tool_groups]
            loaded = asyncio.run(agent.toolkit._get_available_skills(agent.state.tool_context.activated_groups))
            if len(loaded) != len(builtin) + len(optional):
                return False
            async def exercise():
                from agentscope.message import ToolCallBlock
                from uuid import uuid4
                for name, arguments in calls:
                    tool_call = ToolCallBlock(id=str(uuid4()), name=name, input=json.dumps(arguments))
                    async for result in agent.toolkit.call_tool(tool_call, agent.state):
                        pass
            asyncio.run(exercise())
            response = asyncio.run(agent.reply(Msg(
                name="user", role="user", content=[TextBlock(text="synthetic input")],
            )))
            expected = {f"tool:{item['server_id']}:{item['tool_name']}" for item in snapshot['tool_grants']}
            if selected_kbs:
                expected.update(('knowledge:SearchKnowledge', 'knowledge:WriteNote', 'knowledge:TraceKnowledge'))
            return expected.issubset(visited) and "synthetic response" in str(response.content)


if __name__ == "__main__":
    try:
        success = _probe(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]))
    except Exception:
        success = False
    print(json.dumps({"ok": success}))
