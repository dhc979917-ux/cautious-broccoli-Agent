"""修正 PG 中遗留的 Windows 路径（mcp_servers.command/args_json + knowledge_bases.vault_dir）。
第4次部署：SQLite 数据迁入 PG 后跑一次。幂等。"""
from app.database import engine
from sqlalchemy import text

with engine.connect() as c:
    # MCP command: E:\...\python.exe -> python3
    c.execute(text("UPDATE mcp_servers SET command='python3' WHERE command LIKE 'E:%'"))
    # MCP args: Windows 反斜杠 -> 正斜杠；E:/.../mcp_servers/x.py -> /app/app/agents/mcp_servers/x.py
    c.execute(text("UPDATE mcp_servers SET args_json = REPLACE(args_json, '\\\\', '/')"))
    c.execute(text(
        "UPDATE mcp_servers SET args_json = REPLACE(args_json, "
        "'/app/app/agents/mcp_servers/', "
        "'/app/app/agents/mcp_servers/') WHERE args_json LIKE '%/mcp_servers/%'"
    ))
    # vault_dir: E:\...\vaults\... -> /vaults/...
    c.execute(text("UPDATE knowledge_bases SET vault_dir = REPLACE(vault_dir, '\\\\', '/') WHERE vault_dir LIKE 'E:%'"))
    c.execute(text(
        "UPDATE knowledge_bases SET vault_dir = REPLACE(vault_dir, "
        "'/vaults', '/vaults') WHERE vault_dir LIKE 'E:/%'"
    ))
    c.commit()
    for label, q in [
        ("mcp command still E:", "SELECT count(*) FROM mcp_servers WHERE command LIKE 'E:%'"),
        ("vault_dir still E:", "SELECT count(*) FROM knowledge_bases WHERE vault_dir LIKE 'E:%'"),
    ]:
        print(label, c.execute(text(q)).scalar())
print("Windows paths fixed")