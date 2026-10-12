"""缓存命中率上报链路 + 压缩节流 回归验证（离线：不连模型、不连库、不起服务）。

背景（2026-09-09 实测）：DashScope 兼容端点确实回传
usage.prompt_tokens_details.cached_tokens，AgentScope 也把它放进了 ChatUsage，但
ModelCallEndEvent 没有该字段，manager 的 getattr(event,'cache_input_tokens',0) 恒为
0，于是平台上报 cache_rate=0.0（session 271 的真实命中率是 60.7%）。本次改动：
  - UsageCaptureMiddleware 在官方 on_model_call 钩子处捕获缓存 token；
  - manager 在 ModelCallEndEvent 处取走并计入 token_usage，同时逐轮打点；
  - HeadroomCompression 改为门控批量压缩（原地改写旧块会让该轮前缀失效）。

用法（任意目录）：
    python test/_test_cache_usage.py

覆盖三组：
  1. 中间件记账：流式（含 usage-only 载体 chunk）/ 非流式 / reply_id 缺省回退 /
     取走即清零（幂等）/ 多 reply 累计与 FIFO 上限 / on_reply 收尾清理。
  2. manager 集成：假 Agent + 假事件类跑真实 _chat_stream_core，done 事件里的
     cache_input_tokens 与 cache_rate 必须是真实值；事件自带 cache 字段时优先用
     事件值且不重复计数（框架将来补字段可无缝切换）。
  3. 压缩节流：满 N 轮才压 / 工具组变化立即压 / 上下文压力立即压 / N<=1 回到每轮都压
     的旧行为 / 跳过的轮次绝不动上下文（前缀保持可缓存）。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.agents.manager import AgentManager  # noqa: E402
from app.agents.middlewares import HeadroomCompressionMiddleware  # noqa: E402
from app.agents.middlewares import UsageCaptureMiddleware  # noqa: E402
from app.agents.middlewares import headroom_compression as hc  # noqa: E402
from app.agents.middlewares import input_budget as ib  # noqa: E402
from app.agents.middlewares.usage_capture import (  # noqa: E402
    forget_reply,
    get_reply_usage,
    take_call_cache_tokens,
)

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' -> ' + str(extra)) if extra else ''}")


# ==================== 公共假对象 ====================
def mk_usage(inp, out, cache, cache_create=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_input_tokens=cache,
        cache_creation_input_tokens=cache_create,
        time=0.1,
    )


def mk_agent(reply_id="r1"):
    return SimpleNamespace(state=SimpleNamespace(reply_id=reply_id, session_id=1))


async def drive_stream_call(mw, agent, usage, with_carrier=True):
    """按框架真实形态跑一次流式模型调用：内容 chunk -> usage-only 载体 -> is_last 累加块。"""
    async def next_handler(**kwargs):
        async def gen():
            yield SimpleNamespace(usage=None, content=["部分文本"], is_last=False)
            if with_carrier:
                yield SimpleNamespace(usage=usage, content=[], is_last=False)
            yield SimpleNamespace(usage=usage, content=[], is_last=True)
        return gen()

    res = await mw.on_model_call(agent, {}, next_handler)
    seen = 0
    async for _chunk in res:
        seen += 1
    return seen


# ==================== 1. 中间件记账 ====================
def test_middleware():
    print("\n[1] UsageCaptureMiddleware 记账")
    mw = UsageCaptureMiddleware()

    agent = mk_agent("r1")
    n = asyncio.run(drive_stream_call(mw, agent, mk_usage(1000, 50, 640)))
    check("流式：三个 chunk 原样透传", n == 3, n)
    check("流式：缓存 token 被记下来", take_call_cache_tokens(agent, "r1") == 640)
    check("幂等：取走后再取为 0（不会重复计数）", take_call_cache_tokens(agent, "r1") == 0)

    agent2 = mk_agent("r2")
    asyncio.run(drive_stream_call(mw, agent2, mk_usage(2000, 80, 1280), with_carrier=False))
    check("流式：无 usage-only 载体时也能从 is_last 块取到",
          take_call_cache_tokens(agent2, "r2") == 1280)

    agent3 = mk_agent("r3")

    async def non_stream():
        async def next_handler(**kwargs):
            return SimpleNamespace(usage=mk_usage(500, 20, 384), content=[], is_last=True)
        return await mw.on_model_call(agent3, {}, next_handler)

    res = asyncio.run(non_stream())
    check("非流式：返回对象原样透传", getattr(res, "is_last", None) is True)
    check("非流式：缓存 token 被记下来", take_call_cache_tokens(agent3, "r3") == 384)

    agent4 = mk_agent("r4")
    asyncio.run(drive_stream_call(mw, agent4, mk_usage(100, 5, 64)))
    check("reply_id 缺省时回退到最近一条", take_call_cache_tokens(agent4, "") == 64)

    agent5 = mk_agent("r5")

    async def multi():
        for i in range(3):
            await drive_stream_call(mw, agent5, mk_usage(1000, 50, 100 * (i + 1)))
            take_call_cache_tokens(agent5, "r5")
    asyncio.run(multi())
    acc = get_reply_usage(agent5, "r5")
    check("同一 reply 多次调用累计", acc["calls"] == 3 and acc["cache_input_tokens"] == 600, acc)
    check("累计里带 input/output（供成本口径复用）",
          acc["input_tokens"] == 3000 and acc["output_tokens"] == 150, acc)

    agent6 = mk_agent()
    for i in range(25):
        agent6.state.reply_id = f"reply-{i}"
        asyncio.run(drive_stream_call(mw, agent6, mk_usage(10, 1, 8)))
    st = getattr(agent6, "_cache_usage_capture")
    check("多 reply 记录有 FIFO 上限（不随会话无限增长）",
          len(st["pending"]) <= 16 and len(st["replies"]) <= 16,
          (len(st["pending"]), len(st["replies"])))

    # 事件生产者是独立 task，可能连跑多轮才被消费侧取走：必须按调用顺序出队
    agent8 = mk_agent("r8")

    async def burst():
        for cache in (100, 200, 300):
            await drive_stream_call(mw, agent8, mk_usage(1000, 10, cache))
    asyncio.run(burst())
    got = [take_call_cache_tokens(agent8, "r8") for _ in range(4)]
    check("同一 reply 连续多轮未被取走时按序出队", got == [100, 200, 300, 0], got)

    forget_reply(agent5, "r5")
    check("forget_reply 清掉累计", get_reply_usage(agent5, "r5")["calls"] == 0)

    agent7 = mk_agent("r7")
    events = asyncio.run(run_reply_flow(mw, agent7))
    check("on_reply 透传全部事件", len(events) == 3, len(events))
    check("on_reply 收尾清理该 reply 的记录",
          get_reply_usage(agent7, "r7")["calls"] == 0 and take_call_cache_tokens(agent7, "r7") == 0)


async def run_reply_flow(mw, agent):
    """跑一遍 on_reply 全链路：内层产出 3 个事件，其中夹一次真实模型调用记账。"""
    async def inner_gen():
        yield SimpleNamespace(reply_id="r7", type="reply_start")
        await drive_stream_call(mw, agent, mk_usage(800, 40, 512))
        yield SimpleNamespace(reply_id="r7", type="model_call_end")
        yield SimpleNamespace(reply_id="r7", type="reply_end")

    # on_reply 的 next_handler 是同步可调用、返回异步生成器（与框架 _budget.py 一致）
    def next_handler(**kwargs):
        return inner_gen()

    out = []
    async for ev in mw.on_reply(agent, {}, next_handler):
        out.append(ev)
    return out


# ==================== 2. manager 集成 ====================
class ModelCallStartEvent:
    """名字必须与框架事件类一致：manager 用 type(event).__name__ 分派。"""

    def __init__(self, reply_id, model_name):
        self.reply_id = reply_id
        self.model_name = model_name
        self.type = "model_call_start"


class ModelCallEndEvent:
    def __init__(self, reply_id, input_tokens, output_tokens, cache_input_tokens=None):
        self.reply_id = reply_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.type = "model_call_end"
        # 框架当前不透出该字段；显式传入时模拟"框架将来补上了"
        if cache_input_tokens is not None:
            self.cache_input_tokens = cache_input_tokens


class ReplyEndEvent:
    def __init__(self, reply_id):
        self.reply_id = reply_id
        self.type = "reply_end"


class FakeAgent:
    """假 Agent：每轮先跑真实的 UsageCaptureMiddleware.on_model_call，再吐框架事件。"""

    def __init__(self, rounds, event_cache=None):
        self.state = SimpleNamespace(reply_id="r1", session_id=None, tasks_context=None)
        self.rounds = rounds            # [(input, output, 真实 cache)]
        self.event_cache = event_cache  # None = 事件不带 cache 字段（当前框架行为）
        self.mw = UsageCaptureMiddleware()

    def reply_stream(self, inputs=None, structured_schema=None, yield_final_msg=False):
        return self._stream()

    async def _stream(self):
        for idx, (inp, out, cache) in enumerate(self.rounds, start=1):
            yield ModelCallStartEvent("r1", "fake-model")
            await drive_stream_call(self.mw, self, mk_usage(inp, out, cache))
            ev_cache = None if self.event_cache is None else self.event_cache[idx - 1]
            yield ModelCallEndEvent("r1", inp, out, cache_input_tokens=ev_cache)
        yield ReplyEndEvent("r1")


def run_manager(rounds, event_cache=None):
    agent = FakeAgent(rounds, event_cache=event_cache)
    mgr = AgentManager()

    async def go():
        out = []
        async for ev in mgr._chat_stream_core(agent, "做一个 10 页 PPT"):
            out.append(ev)
        return out

    return asyncio.run(go())


def test_manager():
    print("\n[2] manager 集成：done 事件里的 token_usage")
    rounds = [(4641, 315, 0), (10398, 203, 4224), (28476, 8295, 16896)]
    events = run_manager(rounds)
    snapshots = [e for e in events if isinstance(e, dict) and e.get('event') == 'usage']
    check('逐轮累计快照与模型调用数一致', len(snapshots) == len(rounds), snapshots)
    if snapshots:
        check('缓存累计快照单调且最终对账',
              [e['token_usage']['cache_input_tokens'] for e in snapshots] == [0, 4224, 21120]
              and snapshots[-1]['token_usage']['input_tokens'] == sum(r[0] for r in rounds))
    done = [e for e in events if isinstance(e, dict) and e.get("event") == "done"]
    check("收到 done 事件", len(done) == 1, len(done))
    if not done:
        return
    tu = done[0]["token_usage"]
    exp_in = sum(r[0] for r in rounds)
    exp_out = sum(r[1] for r in rounds)
    exp_cache = sum(r[2] for r in rounds)
    check("input/output 口径未变", tu["input_tokens"] == exp_in and tu["output_tokens"] == exp_out, tu)
    check("cache_input_tokens 为真实值（不再是 0）", tu["cache_input_tokens"] == exp_cache, tu)
    check("cache_rate = cache/input", tu["cache_rate"] == round(exp_cache / exp_in * 100, 1), tu)
    check("轮数统计未受影响", done[0]["model_call_rounds"] == len(rounds), done[0])

    # 框架将来在事件里带上 cache 字段：优先用事件值，且不能与中间件记录叠加
    events2 = run_manager([(1000, 10, 400), (2000, 20, 800)], event_cache=[400, 800])
    done2 = [e for e in events2 if isinstance(e, dict) and e.get("event") == "done"][0]
    check("事件自带 cache 字段时不重复计数",
          done2["token_usage"]["cache_input_tokens"] == 1200, done2["token_usage"])

    # 全 0 命中（例如端点不上报缓存的自研模型）：口径仍要正确、不报错
    events3 = run_manager([(500, 5, 0), (600, 6, 0)])
    done3 = [e for e in events3 if isinstance(e, dict) and e.get("event") == "done"][0]
    check("零命中时 cache_rate=0 且不异常",
          done3["token_usage"]["cache_input_tokens"] == 0 and done3["token_usage"]["cache_rate"] == 0,
          done3["token_usage"])


# ==================== 3. 压缩节流 ====================
def mk_ctx(n_thinking=2, size=1200):
    """构造一个够长的上下文：n 条消息，每条带一个超长 thinking 块。"""
    ctx = [SimpleNamespace(role="user", content="请做一个 10 页 PPT")]
    for i in range(n_thinking):
        ctx.append(SimpleNamespace(
            role="assistant",
            name="agent",
            content=[SimpleNamespace(thinking="思考" * size, id=f"think-{i}", output=None)],
        ))
    return ctx


def mk_cagent(ctx, context_size=131072, groups=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            context=ctx,
            session_id=1,
            tool_context=SimpleNamespace(activated_groups=list(groups or [])),
        ),
        model=SimpleNamespace(context_size=context_size),
        offloader=None,
    )


class _FakeSettings:
    def __init__(self, every_n, pressure):
        self.CONTEXT_COMPRESS_EVERY_N_ROUNDS = every_n
        self.CONTEXT_COMPRESS_PRESSURE_RATIO = pressure
        self.HEADROOM_CRUSH_TIMEOUT_SECONDS = 20.0


def run_rounds(agent, rounds, every_n=3, pressure=0.5, grow=True):
    """跑 rounds 轮 on_reasoning 前置压缩，返回每轮是否发生原地改写。"""
    real_settings = hc.settings
    hc.settings = _FakeSettings(every_n, pressure)
    mw = HeadroomCompressionMiddleware()
    mutated_rounds = []
    try:
        for i in range(rounds):
            before = hc.gate_stats(agent)["mutated_blocks"]
            asyncio.run(mw._maybe_compress(agent))
            after = hc.gate_stats(agent)["mutated_blocks"]
            mutated_rounds.append(after > before)
            if grow:
                # 模拟每轮新增一个待压缩块（真实场景：新一轮的 thinking/工具结果）
                agent.state.context.append(SimpleNamespace(
                    role="assistant",
                    name="agent",
                    content=[SimpleNamespace(thinking="思考" * 1200, id=f"think-grow-{i}",
                                             output=None)],
                ))
    finally:
        hc.settings = real_settings
    return mutated_rounds, hc.gate_stats(agent)


def test_compression_gate():
    print("\n[3] HeadroomCompression 节流门控")

    agent = mk_cagent(mk_ctx())
    mutated, st = run_rounds(agent, 6, every_n=3)
    check("N=3：6 轮里只在第 1、4 轮改写上下文", mutated == [True, False, False, True, False, False], mutated)
    check("N=3：压缩扫描次数与改写轮次一致", st["compressions"] == 2, st)

    agent1 = mk_cagent(mk_ctx())
    mutated1, st1 = run_rounds(agent1, 4, every_n=1)
    check("N=1：退回每轮都压的旧行为", mutated1 == [True, True, True, True], mutated1)
    check("N=1：扫描次数=轮数", st1["compressions"] == 4, st1)

    # 跳过的轮次绝不能动上下文（前缀必须保持可缓存）
    agent2 = mk_cagent(mk_ctx(n_thinking=1))
    real_settings = hc.settings
    hc.settings = _FakeSettings(5, 0.0)
    mw = HeadroomCompressionMiddleware()
    try:
        asyncio.run(mw._maybe_compress(agent2))          # 第 1 轮：压
        agent2.state.context.append(SimpleNamespace(
            role="assistant", name="agent",
            content=[SimpleNamespace(thinking="思考" * 1200, id="think-x", output=None)],
        ))
        asyncio.run(mw._maybe_compress(agent2))          # 第 2 轮：应跳过
        skipped = agent2.state.context[-1].content[0]
        check("跳过的轮次不改写旧块（前缀稳定）", len(skipped.thinking) == 2400, len(skipped.thinking))
    finally:
        hc.settings = real_settings

    # 工具组变化：该轮前缀已失效，压缩"免费"，必须立刻做
    agent3 = mk_cagent(mk_ctx(n_thinking=1))
    real_settings = hc.settings
    hc.settings = _FakeSettings(10, 0.0)
    try:
        mw3 = HeadroomCompressionMiddleware()
        b0 = hc.gate_stats(agent3)["mutated_blocks"]
        asyncio.run(mw3._maybe_compress(agent3))         # 第 1 轮：记基线 + 压
        agent3.state.context.append(SimpleNamespace(
            role="assistant", name="agent",
            content=[SimpleNamespace(thinking="思考" * 1200, id="think-t", output=None)],
        ))
        agent3.state.tool_context.activated_groups = ["file_ops", "skill_powerpoint"]
        asyncio.run(mw3._maybe_compress(agent3))         # 第 2 轮：工具组变了 -> 立即压
        b1 = hc.gate_stats(agent3)["mutated_blocks"]
        check("工具组变化的轮次立即压缩（不等 N 轮）", b1 > b0, (b0, b1))
    finally:
        hc.settings = real_settings

    # 上下文压力：超过 context_size * ratio 的轮次立即压（不等 N 轮）
    agent4 = mk_cagent(mk_ctx(n_thinking=6, size=3000), context_size=8000)
    real_settings = hc.settings
    hc.settings = _FakeSettings(100, 0.5)
    try:
        mw4 = HeadroomCompressionMiddleware()
        asyncio.run(mw4._maybe_compress(agent4))         # 第 1 轮：首轮必压，压完上下文变小
        for i in range(6):                               # 再灌入新的大块，把压力顶回阈值以上
            agent4.state.context.append(SimpleNamespace(
                role="assistant", name="agent",
                content=[SimpleNamespace(thinking="思考" * 3000, id=f"think-p-{i}",
                                         output=None)],
            ))
        asyncio.run(mw4._maybe_compress(agent4))         # 第 2 轮：N=100 未到，但压力触发
        st4 = hc.gate_stats(agent4)
        check("压力轮立即压缩（不等 N 轮）", st4["compressions"] == 2, st4)
    finally:
        hc.settings = real_settings

    # 压力门关闭（ratio=0）时只看轮次
    agent5 = mk_cagent(mk_ctx(n_thinking=6, size=3000), context_size=8000)
    real_settings = hc.settings
    hc.settings = _FakeSettings(3, 0.0)
    try:
        mw5 = HeadroomCompressionMiddleware()
        asyncio.run(mw5._maybe_compress(agent5))
        asyncio.run(mw5._maybe_compress(agent5))
        check("ratio=0 关闭压力门后按轮次节流", hc.gate_stats(agent5)["compressions"] == 1,
              hc.gate_stats(agent5))
    finally:
        hc.settings = real_settings

    # 上下文太短：完全不动（保护最近交互）
    agent6 = mk_cagent([SimpleNamespace(role="user", content="你好")])
    real_settings = hc.settings
    hc.settings = _FakeSettings(1, 0.5)
    try:
        mw6 = HeadroomCompressionMiddleware()
        asyncio.run(mw6._maybe_compress(agent6))
        check("上下文短于阈值时不压缩也不计轮", hc.gate_stats(agent6)["rounds"] == 0,
              hc.gate_stats(agent6))
    finally:
        hc.settings = real_settings


def test_input_budget_calibration():
    print('\n[4] 单次输入实报校准（不降低回复上限）')
    agent = SimpleNamespace(state=SimpleNamespace(
        context=[SimpleNamespace(content='x' * 90000)], session_id=1,
    ))
    compressed = []

    async def compress_context():
        compressed.append(True)
        agent.state.context = [SimpleNamespace(content='保留最近上下文')]

    async def next_handler(**kwargs):
        if False:
            yield None

    agent.compress_context = compress_context
    original_settings = ib.settings
    ib.settings = SimpleNamespace(CONTEXT_INPUT_HARD_LIMIT_TOKENS=45000)
    try:
        middleware = ib.ContextInputLimitMiddleware()

        async def run_round():
            async for _ in middleware.on_reasoning(agent, {}, next_handler):
                pass

        asyncio.run(run_round())
        check('首轮未误压低于门槛的上下文', not compressed)
        UsageCaptureMiddleware._commit(agent, mk_usage(56000, 100, 12000))
        check('估算差值捕获系统提示和工具开销', agent._input_budget_overhead == 26000)
        asyncio.run(run_round())
        check('下一轮用真实开销触发压缩而非硬截断', len(compressed) == 1)
    finally:
        ib.settings = original_settings


def test_full_session_usage():
    import json
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models.message import Message
    from app.services.session_service import SessionService

    engine = create_engine('sqlite:///:memory:')
    Message.__table__.create(engine)
    try:
        with Session(engine) as db:
            for message_id in range(60):
                usage = {'input_tokens': 100, 'output_tokens': 10, 'cache_input_tokens': 0}
                db.add(Message(session_id=77, role='agent', content='', metadata_json=json.dumps({
                    'token_usage': usage, 'run_id': f'run-{message_id}',
                })))
            db.add(Message(session_id=78, role='agent', content='', metadata_json=json.dumps({
                'token_usage': {'input_tokens': 900, 'output_tokens': 50, 'cache_input_tokens': 700},
            })))
            db.commit()
            service = SessionService(db)
            visible, has_more = service.get_session_messages(77)
            totals = service.get_session_usage(77)
            check('只读 50 条历史却汇总全部 60 条', len(visible) == 50 and has_more and totals['input_tokens'] == 6000)
            check('跨会话隔离与真实零命中', totals['output_tokens'] == 600 and totals['cache_rate'] == 0)
            check('累计值包含最后一次运行标识', totals['last_run_id'] == 'run-59')
    finally:
        engine.dispose()


def main():
    print("==== 缓存命中率上报链路 回归验证（离线）====")
    test_middleware()
    test_manager()
    test_compression_gate()
    test_input_budget_calibration()
    test_full_session_usage()

    passed = sum(1 for _n, ok in results if ok)
    print(f"\n==== {passed}/{len(results)} 项通过 ====")
    for name, ok in results:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
