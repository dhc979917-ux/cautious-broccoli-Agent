"""工具失败观测中间件（ToolErrorLogMiddleware）回归验证（离线：不连模型、不连库）。

背景（2026-09-10 会话 276 排查实录）：框架 TracingMiddleware 只在工具**抛异常**时
把 span 标 ERROR（middleware/_tracing/_trace.py:337 起，state=error 的正常返回值走
_set_span_success_status），而内置工具的失败几乎都是 ToolChunk(state=ERROR)；平台侧
只有 manager 汇总的 tool_error_count 计数，看不出"哪个工具、哪一轮、报了什么"。
本次改动：on_acting 钩子上原样透传 chunk，仅在 error/denied 时补一条 WARNING 日志。

用法（任意目录）：
    python test/_test_tool_error_log.py

覆盖三组：
  1. 透传安全性：chunk 顺序/内容/身份一字不改；成功调用不打日志；异常原样冒泡。
  2. 日志内容：ERROR / DENIED 都记，SUCCESS / RUNNING / INTERRUPTED 不记；
     多个 error chunk 只取第一个的原因；带上 session/reply/iter/tool/input/reason。
  3. 健壮性与接线：入参不可 JSON 序列化、内容为空、agent.state 缺字段都不炸；
     框架的 is_implemented("on_acting") 能识别（否则中间件不会被挂上）。
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from agentscope.message import TextBlock, ToolResultState  # noqa: E402
from agentscope.tool._response import ToolChunk, ToolResponse  # noqa: E402

from app.agents.middlewares import ToolErrorLogMiddleware  # noqa: E402
from app.agents.middlewares.tool_error_log import (  # noqa: E402
    _input_brief,
    _reason,
    _tool_name,
)

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' -> ' + str(extra)) if extra else ''}")


class CaptureHandler(logging.Handler):
    """收走中间件打的日志，断言内容与条数。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


def install_capture():
    cap = CaptureHandler()
    log = logging.getLogger("app.agents.middlewares.tool_error_log")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.addHandler(cap)
    log.propagate = False
    return cap


def mk_agent(session_id="276", reply_id="r-abc", cur_iter=7):
    return SimpleNamespace(
        state=SimpleNamespace(
            session_id=session_id, reply_id=reply_id, cur_iter=cur_iter,
        ),
    )


def mk_tool_call(name="Edit", **inp):
    return {"type": "tool_use", "id": "call_1", "name": name, "input": inp}


def chunk(state, text=""):
    return ToolChunk(
        content=[TextBlock(type="text", text=text)] if text else [],
        state=state,
        is_last=state != ToolResultState.RUNNING,
    )


def response(state, text=""):
    return ToolResponse(
        content=[TextBlock(type="text", text=text)] if text else [],
        state=state,
    )


class _NoToolCall:
    """显式表示"框架没给 tool_call"，与 None 区分开（None 会被默认值顶掉）。"""


NO_TOOL_CALL = _NoToolCall()


async def drive(items, tool_call=None, exc=None, agent=None, collect_from=None):
    """按框架 execute_chain 的真实形态跑一次 on_acting。"""
    mw = ToolErrorLogMiddleware()
    agent = agent if agent is not None else mk_agent()
    if tool_call is None:
        tool_call = mk_tool_call()
    input_kwargs = {} if isinstance(tool_call, _NoToolCall) else {"tool_call": tool_call}
    # produced：内层真实产出的对象；yielded：中间件吐给上层的对象。分开收，
    # 才能断言"透传的是同一批对象"而不是被复制/改写。
    produced = []
    yielded = []

    def next_handler(**kwargs):
        async def gen():
            for it in items:
                produced.append(it)
                yield it
            if exc is not None:
                raise exc
        return gen()

    async for item in mw.on_acting(
        agent=agent,
        input_kwargs=input_kwargs,
        next_handler=next_handler,
    ):
        yielded.append(item)
    if collect_from is not None:
        collect_from.extend(yielded)
    return yielded


# ==================== 1. 透传安全性 ====================
async def group_passthrough():
    print("\n[1] 透传安全性")
    cap = install_capture()

    items = [
        chunk(ToolResultState.RUNNING, "部分输出"),
        chunk(ToolResultState.RUNNING, "更多输出"),
        response(ToolResultState.SUCCESS, "done"),
    ]
    seen = []
    out = await drive(items, collect_from=seen)
    check("成功调用：chunk 数量与顺序不变", len(out) == len(items), len(out))
    check("成功调用：yield 的是同一批对象（未复制/未改写）",
          all(a is b for a, b in zip(items, out)) and seen == out)
    check("成功调用：不打日志", cap.messages() == [], cap.messages())

    cap.records.clear()
    err_items = [chunk(ToolResultState.ERROR, "Error: To edit a file, you must first read it.")]
    seen2 = []
    out2 = await drive(err_items, collect_from=seen2)
    check("失败调用：错误 chunk 原样透传",
          out2[0] is err_items[0] and out2[0].state == ToolResultState.ERROR)

    cap.records.clear()
    boom = RuntimeError("backend exploded")
    raised = None
    try:
        await drive([chunk(ToolResultState.RUNNING, "x")], exc=boom)
    except RuntimeError as e:
        raised = e
    check("工具抛异常时原样冒泡（不吞异常）", raised is boom, raised)
    check("异常路径不误报 tool_error（框架另有 span ERROR 标记）",
          cap.messages() == [], cap.messages())


# ==================== 2. 日志内容 ====================
async def group_logging():
    print("\n[2] 日志内容")
    cap = install_capture()

    await drive([response(ToolResultState.ERROR, "Error: old_string not found in /workspace/a.py\n请重试")],
                tool_call=mk_tool_call("Edit", file_path="/workspace/a.py", old_string="x"))
    msgs = cap.messages()
    check("ERROR 记一条 WARNING", len(msgs) == 1 and cap.records[0].levelno == logging.WARNING, msgs)
    m = msgs[0] if msgs else ""
    check("日志含 [tool_error] 前缀", m.startswith("[tool_error]"), m[:40])
    check("日志含 session/reply/iter", "session=276" in m and "reply=r-abc" in m and "iter=7" in m, m)
    check("日志含工具名与状态", "tool=Edit" in m and "state=error" in m, m)
    check("日志含入参摘要", "file_path" in m and "/workspace/a.py" in m, m)
    check("日志只取错误首行", "old_string not found" in m and "请重试" not in m, m)

    cap.records.clear()
    await drive([response(ToolResultState.DENIED, "Error: 命令被安全策略拦截（关机/重启）")],
                tool_call=mk_tool_call("Bash", command="shutdown -r"))
    check("DENIED 也记一条（守卫拦截同样需要可见）",
          len(cap.messages()) == 1 and "state=denied" in cap.messages()[0], cap.messages())

    for state in (ToolResultState.SUCCESS, ToolResultState.RUNNING):
        cap.records.clear()
        await drive([response(state, "ok")] if state != ToolResultState.RUNNING
                    else [chunk(state, "ok")])
        check(f"{getattr(state, 'value', state)} 不记日志", cap.messages() == [], cap.messages())

    cap.records.clear()
    await drive([
        chunk(ToolResultState.ERROR, "第一处错误"),
        chunk(ToolResultState.ERROR, "第二处错误"),
        response(ToolResultState.ERROR, "第一处错误"),
    ])
    check("多个 error chunk 只记一条，原因取第一个",
          len(cap.messages()) == 1 and "第一处错误" in cap.messages()[0]
          and "第二处错误" not in cap.messages()[0], cap.messages())

    # 流被提前关闭（消费方 break）：finally 里仍应记录
    cap.records.clear()
    mw = ToolErrorLogMiddleware()

    def next_handler(**kwargs):
        async def gen():
            yield chunk(ToolResultState.ERROR, "Error: 沙箱不可用")
            yield response(ToolResultState.ERROR, "Error: 沙箱不可用")
        return gen()

    stream = mw.on_acting(agent=mk_agent(),
                          input_kwargs={"tool_call": mk_tool_call("Bash")},
                          next_handler=next_handler)
    async for _item in stream:
        break
    # async generator 的 finally 在 aclose() 时才跑（break 后由事件循环延迟回收）
    await stream.aclose()
    check("消费方提前关闭流也会记录（finally 兜底）",
          len(cap.messages()) == 1, cap.messages())


# ==================== 3. 健壮性与接线 ====================
async def group_robustness():
    print("\n[3] 健壮性与接线")
    cap = install_capture()

    class Unserializable:
        def __repr__(self):
            return "<obj>"

    await drive([response(ToolResultState.ERROR, "boom")],
                tool_call=mk_tool_call("Write", content=Unserializable()))
    check("入参不可 JSON 序列化时降级为 str，不抛异常",
          len(cap.messages()) == 1 and "<obj>" in cap.messages()[0], cap.messages())

    cap.records.clear()
    await drive([response(ToolResultState.ERROR, "")], tool_call=mk_tool_call("Read"))
    check("错误正文为空时给出占位符",
          "<无文本输出>" in cap.messages()[0], cap.messages())

    cap.records.clear()
    bare = SimpleNamespace(state=None)
    await drive([response(ToolResultState.ERROR, "x")], agent=bare)
    check("agent.state 缺失时不炸", len(cap.messages()) == 1, cap.messages())

    cap.records.clear()
    await drive([response(ToolResultState.ERROR, "x")], tool_call=NO_TOOL_CALL)
    check("tool_call 缺失时不炸并标 <unknown>",
          len(cap.messages()) == 1 and "tool=<unknown>" in cap.messages()[0], cap.messages())

    # 纯函数边界
    check("_tool_name 兼容 dict 与对象两种形态",
          _tool_name({"name": "Edit"}) == "Edit"
          and _tool_name(SimpleNamespace(name="Read")) == "Read"
          and _tool_name(None) == "")
    long_in = {"command": "y" * 1000}
    check("_input_brief 超长入参截断", len(_input_brief({"name": "Bash", "input": long_in})) <= 303)
    check("_input_brief 空入参返回空串", _input_brief({"name": "Bash", "input": {}}) == "")
    check("_reason 兼容 dict 形态的 content block",
          _reason(SimpleNamespace(content=[{"text": "首行\n次行"}])) == "首行")
    check("_reason 跳过空行取第一条非空行",
          _reason(SimpleNamespace(content=[{"text": "\n  \n真正的原因"}])) == "真正的原因")
    check("_reason 无内容返回空串", _reason(SimpleNamespace(content=[])) == "")
    check("_reason 长错误截断", len(_reason(response(ToolResultState.ERROR, "z" * 900))) <= 403)

    # 接线：框架靠 is_implemented 决定是否把中间件挂进 _acting_middlewares
    check("框架能识别 on_acting 已实现（否则不会被挂上）",
          ToolErrorLogMiddleware().is_implemented("on_acting") is True)
    check("未实现的钩子不被误报（不影响 reasoning/model_call 链路）",
          ToolErrorLogMiddleware().is_implemented("on_reasoning") is False
          and ToolErrorLogMiddleware().is_implemented("on_model_call") is False)

    # 中间件栈接线：base.py 的构建结果里必须有它
    import inspect
    from app.agents.architectures import base as arch_base
    src = inspect.getsource(arch_base.AgentBuilderBase._build_middlewares)
    check("base.py 中间件栈已挂 ToolErrorLogMiddleware", "ToolErrorLogMiddleware()" in src)


async def main():
    await group_passthrough()
    await group_logging()
    await group_robustness()

    failed = [n for n, ok in results if not ok]
    print(f"\n===== 合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)} =====")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
