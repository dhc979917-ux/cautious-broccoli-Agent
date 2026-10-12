# -*- coding: utf-8 -*-
# 验证 MCP schema 精简是否生效：逐 server 打印工具描述长度分布（容器内跑）
# docker exec -e PYTHONPATH=/app <backend> python /tmp/_tk_verify_slim.py
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO)


async def main():
    from app.database import SessionLocal
    from app.services.mcp_service import McpService
    from app.agents.mcp_cache import CachingMCPClient

    db = SessionLocal()
    try:
        recs = McpService(db).get_by_ids([2, 3, 4, 5, 6, 7, 9])
        ids = [r.id for r in recs]
        clients = await McpService(db).build_clients(ids)
        clients = [CachingMCPClient(c, slim=True) for c in clients]
        grand = 0
        for c in clients:
            raw = await c.list_raw_tools()
            descs = [len(t.description or "") for t in raw]
            total = sum(
                len(t.name or "") + len(t.description or "")
                + len(str(getattr(t, "inputSchema", {}) or {}))
                for t in raw
            )
            grand += total
            print(f"[slim] {c.name:<25} tools={len(raw):<3} "
                  f"desc max={max(descs) if descs else 0:<5} "
                  f"desc>300: {sum(1 for d in descs if d > 301):<3} "
                  f"总字符={total}")
        print(f"[slim] 合计 {grand} 字符（改造前实测 71,014）")
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass
        return 0
    finally:
        db.close()


sys.exit(asyncio.run(main()))
