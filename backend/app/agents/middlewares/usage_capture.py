"""UsageCaptureMiddleware：补齐框架事件流里缺失的缓存命中 token。

为什么需要（2026-09-09 实测结论）：
- DashScope 的 OpenAI 兼容端点确实回传 ``usage.prompt_tokens_details.cached_tokens``，
  AgentScope 把它放进 ``ChatUsage.cache_input_tokens``，TracingMiddleware 也会写进
  span 属性 ``agentscope.usage.cache_input_tokens``（traces 里数据完整）；
- 但 Agent 构造 ``ModelCallEndEvent`` 时只透传 input/output tokens，事件类本身没有
  cache 字段，上层 ``getattr(event, "cache_input_tokens", 0)`` 恒为 0；
- 于是平台上报的 cache_rate 恒为 0.0（session 271 实测真实命中率 60.7%）。

做法：用官方 ``on_model_call`` 钩子包住模型调用，从流式响应的累加 chunk
（``is_last=True``，其 usage 已吸收 usage-only 载体 chunk，见 agentscope
``model/_base.py`` 的 ``_stream`` 与 ``ChatResponse.append_chat_response``）读取缓存
token，按 reply_id 记在 Agent 实例上；上层通过本模块的函数取用（取走即清零，天然
幂等；框架将来若在事件里补上该字段，上层优先用事件值即可，本中间件自动退化为空转）。

只实现 ``on_model_call`` / ``on_reply`` 两个钩子：不改框架、不改事件类、不干预推理
与工具执行，与其它中间件互不影响。
"""

import inspect
import logging
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# Agent 实例上存放统计的属性名（下划线前缀，避开框架字段；与 _mcp_clients 同款做法）
_STATE_ATTR = "_cache_usage_capture"
# 最多保留多少个 reply 的记录：HITL 暂停/异常收尾不一定走 ReplyEndEvent，兜底防堆积
_MAX_REPLIES = 16
# 单个 reply 最多缓存多少次未取走的调用记录：事件生产者是独立 task，可能跑在消费
# 侧前面（manager 的事件经队列转发），因此按调用顺序排队而不是只留最后一次
_MAX_PENDING_CALLS = 64


def _empty_acc() -> dict:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _annotate_span(usage) -> None:
    """Write cache usage onto the current OTel LLM span, if any.

    TracingMiddleware creates the model-call span around this middleware, so
    the span is current when usage is committed (both streaming and sync).
    Trace files previously only had input/output tokens, which made cache hit
    rate invisible in offline analysis.
    """
    try:
        from opentelemetry.trace import get_current_span
        span = get_current_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute(
            "gen_ai.usage.cache_input_tokens",
            int(getattr(usage, "cache_input_tokens", 0) or 0),
        )
        span.set_attribute(
            "gen_ai.usage.cache_creation_input_tokens",
            int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        )
    except Exception:
        pass


def _state(agent) -> dict:
    """取（或建）Agent 上的统计容器。建不了就返回一次性容器，绝不阻断推理。"""
    st = getattr(agent, _STATE_ATTR, None)
    if isinstance(st, dict):
        # 兼容旧结构：补累计字段（HITL 错位修复引入，见 take_cache_tokens_delta）
        st.setdefault("total", _empty_acc())
        st.setdefault("taken_cache", 0)
        return st
    st = {"pending": {}, "replies": {}, "total": _empty_acc(), "taken_cache": 0}
    try:
        setattr(agent, _STATE_ATTR, st)
    except Exception as e:  # 极端情况（__slots__/冻结对象）：退化为不记账
        logger.debug("[usage_capture] 无法在 Agent 上挂统计容器（忽略）: %s", e)
    return st


def _prune(d: dict) -> None:
    """按插入序裁掉最旧的 reply 记录（dict 保序），防止长会话状态无限增长。"""
    while len(d) > _MAX_REPLIES:
        d.pop(next(iter(d)), None)


def _record(agent, usage, reply_id: str) -> None:
    """把一次模型调用的 usage 记进 Agent：pending（按序待取走）+ replies（累计）。"""
    if usage is None:
        return
    entry = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_input_tokens": int(getattr(usage, "cache_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
    }
    st = _state(agent)
    queue = st["pending"].setdefault(reply_id, [])
    queue.append(entry)
    # 消费侧长期不取（例如超时收尾）时丢最旧的，保证不会无界增长
    del queue[:-_MAX_PENDING_CALLS]
    acc = st["replies"].setdefault(reply_id, _empty_acc())
    acc["calls"] += 1
    for k in ("input_tokens", "output_tokens",
              "cache_input_tokens", "cache_creation_input_tokens"):
        acc[k] += entry[k]
    # 累计总量（跨 reply、跨 HITL 暂停-恢复只增不减）：cache 统计的可靠口径。
    # 旧 FIFO 队列在 HITL 暂停时会被 on_reply 清掉，且事件生产/消费错位时按序
    # 出队会整轮记丢（session 386 实测 DB 缓存率 57.7% vs trace 实测 76.5%）；
    # 累计量不依赖 reply_id 对齐与出队时序，天然免疫这两类错位。
    total = st.setdefault("total", _empty_acc())
    total["calls"] += 1
    for k in ("input_tokens", "output_tokens",
              "cache_input_tokens", "cache_creation_input_tokens"):
        total[k] += entry[k]
    _prune(st["pending"])
    _prune(st["replies"])


def take_cache_tokens_delta(agent) -> int:
    """取走"自上次调用以来新记录的缓存命中 token"增量（取走即清零，幂等）。

    与 take_call_cache_tokens 的区别：不按 reply_id 对齐、不依赖 FIFO 顺序，
    因此 HITL 暂停/恢复、事件生产消费错位都不会丢轮次。上层（manager）每收到
    一个 ModelCallEndEvent 调一次即可；回复收尾时再补调一次兜底末轮竞态。
    """
    st = _state(agent)
    total = int(st.get("total", {}).get("cache_input_tokens", 0) or 0)
    taken = int(st.get("taken_cache", 0) or 0)
    st["taken_cache"] = total
    return max(0, total - taken)


def discard_cache_tokens_delta(agent) -> None:
    """丢弃当前未取走的缓存增量（框架事件自带 cache 字段时调用，避免双重计数）。"""
    st = _state(agent)
    st["taken_cache"] = int(st.get("total", {}).get("cache_input_tokens", 0) or 0)


def take_strict_call_cache_tokens(agent, reply_id: str):
    '''Take a per-call cache value only when the reply id matches exactly.'''
    queue = (_state(agent).get('pending') or {}).get(reply_id)
    if not queue:
        return None
    return int(queue.pop(0).get('cache_input_tokens') or 0)


def take_call_cache_tokens(agent, reply_id: str = "") -> int:
    """取走指定 reply 最近一次模型调用的缓存命中 token（取过即清零，幂等）。

    按调用顺序 FIFO 出队：事件生产者可能跑在消费侧前面，同一 reply 的多轮记录必须
    排队而不互相覆盖。reply_id 对不上时回退到最近有记录的 reply，兼容框架更换/缺省
    reply_id 的情况。上层每收到一个 ModelCallEndEvent 调一次即可，不会重复计数。
    """
    pending = _state(agent).get("pending") or {}
    if not pending:
        return 0
    if pending.get(reply_id):
        queue = pending[reply_id]
    else:
        queue = next((q for q in reversed(list(pending.values())) if q), None)
        if queue is None:
            return 0
    entry = queue.pop(0)
    return int(entry.get("cache_input_tokens") or 0)


def get_reply_usage(agent, reply_id: str = "") -> dict:
    """本次回复累计的 usage 快照（不清零）。reply_id 为空时汇总全部记录。

    供成本/预算口径复用（例如按缓存折扣计费），只读，不影响事件流。
    """
    replies = _state(agent).get("replies") or {}
    if reply_id:
        src = [replies.get(reply_id)]
    else:
        src = list(replies.values())
    out = _empty_acc()
    for acc in src:
        if not acc:
            continue
        for k in out:
            out[k] += int(acc.get(k) or 0)
    return out


def forget_reply(agent, reply_id: str) -> None:
    """清掉某个 reply 的记录（回复正常收尾时由 on_reply 调用）。"""
    if not reply_id:
        return
    st = _state(agent)
    st.get("pending", {}).pop(reply_id, None)
    st.get("replies", {}).pop(reply_id, None)


class UsageCaptureMiddleware(MiddlewareBase):
    """在模型调用处捕获缓存命中 token，供上层统计真实缓存率。"""

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., object],
    ):
        res = await next_handler(**input_kwargs)
        # 流式：包一层生成器，逐 chunk 记录后原样透传（不改内容、不改顺序）
        if inspect.isasyncgen(res):
            return self._wrap_stream(agent, res)
        self._commit(agent, getattr(res, "usage", None))
        return res

    async def _wrap_stream(self, agent, stream) -> AsyncGenerator:
        """透传流式 chunk，并把"最后一次出现的 usage"作为本次调用的结果记一次。

        一次调用可能有多于一个 chunk 带 usage（框架的 is_last 累加块必然带，部分
        端点还会在中间 chunk 上带），逐 chunk 记账会把一次调用算成多次，因此只在
        流结束（含消费方提前关闭）时提交一次。
        """
        last_usage = None
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    last_usage = usage
                yield chunk
        finally:
            self._commit(agent, last_usage)

    @staticmethod
    def _commit(agent, usage) -> None:
        if usage is None:
            return
        try:
            from app.agents.middlewares.input_budget import record_input_usage
            record_input_usage(agent, int(getattr(usage, 'input_tokens', 0) or 0))
        except Exception:
            pass
        _annotate_span(usage)
        reply_id = str(getattr(getattr(agent, "state", None), "reply_id", "") or "")
        try:
            _record(agent, usage, reply_id)
        except Exception as e:  # 记账失败不能影响回复
            logger.debug("[usage_capture] 记录 usage 失败（忽略）: %s", e)

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        seen: set = set()
        try:
            async for event in next_handler(**input_kwargs):
                rid = getattr(event, "reply_id", None)
                if rid:
                    seen.add(str(rid))
                yield event
        finally:
            # 只清流过本回复的 reply_id；HITL 暂停后恢复会重新记录，
            # 上层（manager）自己在整轮对话里累加，不依赖这里的累计值。
            for rid in seen:
                try:
                    forget_reply(agent, rid)
                except Exception:
                    pass
