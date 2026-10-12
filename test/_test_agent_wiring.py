"""真实 Agent 装配的冒烟测试（需要 app.db，跑完即删）。

验证的不是单元逻辑，而是"接线"：新工具组、新中间件、改过的提示词
是否真的出现在一个按生产路径构建出来的 Agent 上。
"""
import asyncio
import os
import sys

# 本脚本在 test/ 下，被测代码在 backend/；把 backend 加进导入路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
# 后端配置里数据库是相对路径 sqlite:///./app.db，必须把 CWD 切到 backend/
# 才能命中开发库；否则会在当前目录建一个空库（实测踩过）。
os.chdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))


async def main() -> int:
    from app.database import SessionLocal
    from app.models.agent import Agent as AgentModel
    from app.agents.manager import AgentManager
    from app.agents import delivery_profile as dp
    from app.agents.middlewares.build_profile import BuildProfileMiddleware, QUALITY_TOOL_GROUP
    from app.agents.quality_prompts import hints_for

    fails = []

    def ck(name, cond, detail=""):
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    db = SessionLocal()
    try:
        am = db.query(AgentModel).filter(AgentModel.agent_type == "task").first()
        if am is None:
            am = db.query(AgentModel).first()
        print(f"== 用 Agent #{am.id} ({am.agent_type}) 构建 ==")

        mgr = AgentManager()
        agent = await mgr.get_or_create_agent(
            am, user_role="admin", selected_skills=[], user_id=1,
            session_id=999001, db=db,
        )

        # 1. 工具组注册
        groups = [g.name for g in getattr(agent.toolkit, "tool_groups", []) or []]
        print(f"  [INFO] tool_groups = {groups}")
        ck("quality 组已注册", QUALITY_TOOL_GROUP in groups, str(groups))

        # 2. 默认未激活（非构建型会话零 schema 开销）
        act = list(getattr(agent.state.tool_context, "activated_groups", []) or [])
        ck("quality 组默认未激活", QUALITY_TOOL_GROUP not in act, str(act))

        # 3. 中间件挂载。框架按钩子把中间件拆进多个列表（_reasoning_middlewares /
        # _acting_middlewares …），没有统一的 .middlewares 属性，这里逐个查。
        reason_mws = [type(m).__name__ for m in (getattr(agent, "_reasoning_middlewares", None) or [])]
        acting_mws = [type(m).__name__ for m in (getattr(agent, "_acting_middlewares", None) or [])]
        print(f"  [INFO] on_reasoning = {reason_mws}")
        print(f"  [INFO] on_acting    = {acting_mws}")
        ck("BuildProfileMiddleware 挂进推理链", "BuildProfileMiddleware" in reason_mws, str(reason_mws))
        ck("BuildProfileMiddleware 挂进工具链（reset_tools 注入用）",
           "BuildProfileMiddleware" in acting_mws, str(acting_mws))
        if "BuildProfileMiddleware" in reason_mws and "ProactiveBudgetMiddleware" in reason_mws:
            ck("档位中间件排在预算中间件之前",
               reason_mws.index("BuildProfileMiddleware") < reason_mws.index("ProactiveBudgetMiddleware"),
               str(reason_mws))
        # 预算中间件的档位参数确实传进去了
        pb = next((m for m in (getattr(agent, "_reasoning_middlewares", None) or [])
                   if type(m).__name__ == "ProactiveBudgetMiddleware"), None)
        if pb is not None:
            ck("预算中间件带 artifact 放大额度", getattr(pb, "_artifact_budget", 0) > 0,
               str(getattr(pb, "_artifact_budget", None)))
        sb = next((m for m in (getattr(agent, "_reasoning_middlewares", None) or [])
                   if type(m).__name__ == "SessionBudgetMiddleware"), None)
        if sb is not None:
            ck("轮数中间件带 artifact 放宽阈值",
               getattr(sb, "_artifact_soft", 0) > getattr(sb, "_soft", 0),
               f"{getattr(sb, '_soft', None)}/{getattr(sb, '_artifact_soft', None)}")

        # 4. 提示词改写生效
        sp = getattr(agent, "sys_prompt", None) or getattr(agent, "system_prompt", "") or ""
        if not sp:
            for attr in ("_sys_prompt", "_system_prompt"):
                sp = getattr(agent, attr, "") or sp
        ck("拿到 system_prompt", len(sp) > 200, f"len={len(sp)}")
        ck("子Agent豁免清单里已移除『写代码』", "简单任务（写代码、生成文档、数据分析）" not in sp)
        ck("决策规则已收窄为『改变已产出的成果』", "会改变已产出的成果" in sp)
        ck("旧的『简单任务不要调用』已删除", "简单任务不要调用" not in sp)
        ck("文件规则区分了 artifact 例外", "网页/看板/可运行产物" in sp)
        ck("artifact 规则未常驻 system_prompt（按档位拼接）",
           "需求采集规则" not in sp, "常驻会造成非构建型会话的无谓开销")
        ck("delivery_kind 判据已进入提示词", "delivery_kind" in sp)

        # 5. 档位解析 + 按档位拼接系统提示词（直接调中间件钩子，不起模型）
        bp = next((m for m in (getattr(agent, "_reasoning_middlewares", None) or [])
                   if isinstance(m, BuildProfileMiddleware)), None)
        ck("取到 BuildProfileMiddleware 实例", bp is not None)
        sp_mws = [type(m).__name__ for m in (getattr(agent, "_system_prompt_middlewares", None) or [])]
        ck("BuildProfileMiddleware 挂进系统提示词管道",
           "BuildProfileMiddleware" in sp_mws, str(sp_mws))
        if bp is not None:
            agent.state.reply_id = "r-smoke-1"
            from agentscope.message import UserMsg, TextBlock
            agent.state.context.append(UserMsg(
                name="user",
                content=[TextBlock(type="text", text="帮我做一个外延颗粒归因看板，单文件 HTML，双击就能打开")],
            ))
            bp._ensure_profile(agent)
            ck("关键词判定为 artifact 档", dp.is_artifact(agent), dp.get(agent))
            ck("quality 组被自动激活",
               QUALITY_TOOL_GROUP in (agent.state.tool_context.activated_groups or []),
               str(agent.state.tool_context.activated_groups))
            sp2 = await bp.on_system_prompt(agent, sp)
            ck("artifact 档系统提示词含采集规则", "需求采集规则" in sp2)
            ck("artifact 档系统提示词含验证规则", "交付前验证规则" in sp2)
            ck("artifact 档系统提示词含假设台账规则", "假设台账" in sp2)
            ck("规则只拼一次（幂等）", sp2.count("需求采集规则") == 1,
               f"count={sp2.count('需求采集规则')}")
            ck("拼接不改动原有提示词", sp2.startswith(sp))

        # 6. 工具实例本身可用
        from app.agents.tools.verify_artifact import VerifyArtifact
        from app.agents.tools.collect_requirements import CollectRequirements
        va = VerifyArtifact(output_dir=os.path.join(os.getcwd(), "outputs"))
        ck("VerifyArtifact 可实例化且只读", va.is_read_only is True and va.name == "VerifyArtifact")
        cr = CollectRequirements()
        ck("CollectRequirements 是外部执行工具", cr.is_external_tool is True)
        ck("CollectRequirements schema 题数上限=8",
           cr.input_schema["properties"]["questions"]["maxItems"] == 8)

        # 7. 非构建型会话：档位应保持 answer，不激活 quality
        agent2 = await mgr.get_or_create_agent(
            am, user_role="admin", selected_skills=[], user_id=1,
            session_id=999002, db=db,
        )
        bp2 = next((m for m in (getattr(agent2, "_reasoning_middlewares", None) or [])
                    if isinstance(m, BuildProfileMiddleware)), None)
        if bp2 is not None:
            from agentscope.message import UserMsg, TextBlock
            agent2.state.reply_id = "r-smoke-2"
            agent2.state.context.append(UserMsg(
                name="user",
                content=[TextBlock(type="text", text="解释一下 SFQR 和 ESFQR 的区别")],
            ))
            bp2._ensure_profile(agent2)
            ck("问答类判定为 answer 档", dp.get(agent2) == dp.DeliveryKind.ANSWER, dp.get(agent2))
            ck("answer 档不激活 quality 组",
               QUALITY_TOOL_GROUP not in (agent2.state.tool_context.activated_groups or []))
            sp3 = await bp2.on_system_prompt(agent2, sp)
            ck("answer 档系统提示词逐字未变（零增量）", sp3 == sp, f"len {len(sp)} -> {len(sp3)}")

        print(f"\n规则块长度（artifact 档一次性成本）：{len(hints_for(dp.DeliveryKind.ARTIFACT))} 字符")
    finally:
        db.close()

    print(f"\n===== 装配冒烟: {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)} =====")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
