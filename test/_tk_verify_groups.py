# -*- coding: utf-8 -*-
# 部署后容器内功能验证：构建 dialog Agent，检查 MCP 按 server 拆组、schema 精简、
# 组激活动态提示是否生效。容器内跑：docker exec -e PYTHONPATH=/app <backend> python /tmp/_tk_verify_groups.py
import asyncio
import json
import sys


async def main():
    from app.database import SessionLocal
    from app.models.agent import Agent as AgentModel
    from app.models.session import Session
    from app.agents.architectures.dialog import ToolAssistantBuilder

    db = SessionLocal()
    try:
        agent_model = db.query(AgentModel).filter(AgentModel.id == 1).first()
        if agent_model is None:
            print("FAIL: agent id=1 不存在")
            return 1
        # 用 session 386 的属主做构建用户（该用户配过邮箱，场景与基线一致）
        sess = db.query(Session).filter(Session.id == 386).first()
        user_id = sess.user_id if sess else None
        print(f"[verify] agent={agent_model.name} user_id={user_id}")

        agent = await ToolAssistantBuilder().build(
            agent_model, user_role="admin", user_id=user_id, session_id=999999, db=db,
        )

        groups = agent.toolkit.tool_groups
        print(f"[verify] 工具组共 {len(groups)} 个：")
        mcp_groups = [g for g in groups if g.name.startswith("mcp")]
        for g in sorted(groups, key=lambda x: x.name):
            tag = "MCP" if g.name.startswith("mcp") else "   "
            print(f"  {tag} {g.name:<40} desc={g.description[:60]}")
        if not any(g.name == "mcp__email" for g in mcp_groups):
            print("FAIL: 缺少 mcp__email 组")
            return 1
        if any(g.name == "mcp" for g in mcp_groups):
            print("FAIL: 仍存在大一统 mcp 组")
            return 1

        # 每 server 组的 schema 大小（精简后）
        print("[verify] 各 MCP 组 schema 大小：")
        total = 0
        for g in sorted(mcp_groups, key=lambda x: x.name):
            schemas = await agent.toolkit.get_tool_schemas(groups=[g.name])
            # 只数本组工具（basic 组会带上 reset_tools 等）；按 client.name 前缀过滤
            # （组名是清洗过的标识符，client.name 可能含连字符，两者不一定相同）
            client_name = g.mcps[0].name if g.mcps else g.name[5:]
            own = [s for s in schemas if s["function"]["name"].startswith(f"mcp__{client_name}__")]
            size = len(json.dumps(own, ensure_ascii=False))
            total += size
            print(f"  {g.name:<40} 工具数={len(own):<3} schema字符={size}")
        print(f"[verify] 全部 MCP schema 合计 {total} 字符（改造前实测 71,014）")

        # 模拟激活 mcp__email：reset_tools 结果应带邮件工作流 instructions
        reset = agent.toolkit.builtin_meta_tool.tool
        from agentscope.state import AgentState
        chunk = await reset.call(agent.state, mcp__email=True)
        text = chunk.content[0].text
        has_email_wf = "邮件处理流程" in text
        print(f"[verify] 激活 mcp__email 后 reset_tools 返回含邮件工作流: {has_email_wf}")
        print(f"[verify] 激活组列表: {agent.state.tool_context.activated_groups}")

        # 系统提示词里不应再有常驻邮件流程（P2-4）
        prompt_has_email = "邮件处理流程" in (getattr(agent, "_system_prompt", "") or "")
        print(f"[verify] 系统提示词常驻邮件流程（应为 False）: {prompt_has_email}")

        ok = (not prompt_has_email) and has_email_wf
        print("[verify] 结果:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        db.close()


sys.exit(asyncio.run(main()))
