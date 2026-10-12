"""ToolResultEvictionMiddleware：旧工具结果驱逐（microcompact，对照 Claude Code）。

为什么需要（2026-09-22 大纲实证）：
- 框架原生 tool_result_limit 只管"大块入站截断"，HeadroomCompressionMiddleware
  只处理 ≥800 字符的块；而会话里大量工具结果是几百字符的小块（Bash 回显、
  Glob 列表、小查询结果），永不处理、无限累积——session 388 的逐轮 input 从
  31k 一路涨到 55k，主要就是这些"小碎块"的累积。
- 本中间件把"最近 N 条消息之外"的小工具结果替换为短占位符（保留工具名与原始
  长度，模型确需内容时可按原参数重新调用），长会话历史增速因此砍半以上。

与 headroom 的分工：headroom 压"大块"（≥800 字符，截断+offload），本中间件清
"小碎块"（直接换占位符，小结果不值得 offload 成文件）。

缓存纪律（关键）：原地改写历史会让该轮 prompt 前缀从改写点起整体失效。因此驱逐
不做每轮扫描，而是与 headroom 共用同一个节流门（_gate_state）：只在"工具组变化
轮 / headroom 已改写的轮次（前缀反正已废）/ 满 N 轮"三类时机成批执行。挂载顺序
必须在 HeadroomCompressionMiddleware 之后，保证判定用的是当轮已更新的节流状态。

幂等：占位符带标记，已驱逐的块不会二次处理；豁免工具（用户选择/需求采集/写操作
确认等结果必须原样保留的工具）经 TOOL_RESULT_EVICT_EXEMPT_TOOLS 配置。
"""

import logging
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

from app.config import settings
from app.agents.middlewares.headroom_compression import _activated_groups, _gate_state

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# 占位符标记（幂等判定 + 给模型的说明）
_EVICTED_MARK = "[历史结果已清理]"
# 已被其它压缩环节处理过的块不再动（避免把"截断提示"再包一层）
_SKIP_MARKERS = ("已压缩：", "<<<TRUNCATED>>>", _EVICTED_MARK)


def _exempt_names() -> set:
    raw = str(getattr(settings, "TOOL_RESULT_EVICT_EXEMPT_TOOLS", "") or "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _is_exempt(tool_name: str, exempt: set) -> bool:
    """精确名或 mcp__<server>__<tool> 的短名部分命中即豁免。"""
    if not tool_name:
        return False
    if tool_name in exempt:
        return True
    short = tool_name.rsplit("__", 1)[-1]
    return short in exempt


class ToolResultEvictionMiddleware(MiddlewareBase):
    """把最近 N 条消息之外的小工具结果替换为短占位符（与 headroom 同门节流）。"""

    def __init__(self) -> None:
        self._processed: set = set()       # 已驱逐的块 id（实例随 Agent，会话级）
        self._groups: tuple | None = None  # 上次驱逐判定时的激活组快照
        self._last_run_round: int | None = None
        self.stats = {"runs": 0, "evicted": 0, "freed_chars": 0}

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        try:
            if getattr(settings, "TOOL_RESULT_EVICT_ENABLED", False):
                await self._maybe_evict(agent)
        except Exception as e:  # 驱逐失败不阻断推理
            logger.debug("[evict] skipped: %s", e)
        async for event in next_handler(**input_kwargs):
            yield event

    # ------------------------------------------------------------------
    async def _maybe_evict(self, agent: "Agent") -> None:
        """节流门：与 headroom 共用 _gate_state（它在本中间件之前执行、已更新当轮
        计数），只在"前缀反正已废/已到成批周期"的轮次动手，其余轮次保持前缀稳定。"""
        ctx = getattr(agent.state, "context", None) or []
        keep_recent = int(getattr(settings, "TOOL_RESULT_EVICT_KEEP_RECENT", 6) or 6)
        if len(ctx) < keep_recent + 2:
            return

        st = _gate_state(agent)
        round_no = int(st.get("rounds", 0) or 0)
        groups = _activated_groups(agent)

        reasons = []
        if self._groups is not None and groups != self._groups:
            reasons.append("tools_changed")
        self._groups = groups
        # headroom 当轮已经原地改写过（last_mutated_round == 当前轮）：前缀已失效，
        # 驱逐搭便车零缓存代价
        if st.get("last_mutated_round") == round_no:
            reasons.append("headroom_mutated")
        every_n = int(getattr(settings, "CONTEXT_COMPRESS_EVERY_N_ROUNDS", 5) or 5)
        if self._last_run_round is None or (round_no - self._last_run_round) >= every_n:
            reasons.append(f"every_{every_n}")

        if not reasons:
            return
        self._last_run_round = round_no
        self.stats["runs"] += 1

        evicted, freed = self._evict_old_blocks(agent, ctx, keep_recent)
        if evicted:
            self.stats["evicted"] += evicted
            self.stats["freed_chars"] += freed
            logger.info(
                "[evict] session=%s round=%s 驱逐 %d 个小工具结果，释放约 %d 字符 reason=%s",
                getattr(agent.state, "session_id", "?"), round_no, evicted, freed,
                "+".join(reasons),
            )

    # ------------------------------------------------------------------
    def _evict_old_blocks(self, agent: "Agent", ctx: list, keep_recent: int) -> tuple:
        """扫描 ctx[:-keep_recent]，把小工具结果块替换为占位符。返回 (驱逐数, 释放字符数)。"""
        min_chars = int(getattr(settings, "TOOL_RESULT_EVICT_MIN_CHARS", 200) or 200)
        exempt = _exempt_names()
        evicted = 0
        freed = 0
        for msg in ctx[:-keep_recent]:
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                if getattr(block, "type", None) != "tool_result":
                    continue
                output = getattr(block, "output", None)
                if not isinstance(output, list):
                    continue
                subs = [s for s in output if getattr(s, "text", None)]
                if not subs:
                    continue
                total = sum(len(s.text) for s in subs)
                if total < min_chars:
                    continue  # 太小不值得换占位符（占位符本身也占 token）
                tool_name = getattr(block, "name", "") or ""
                if _is_exempt(tool_name, exempt):
                    continue
                block_id = getattr(block, "id", None) or id(block)
                if block_id in self._processed:
                    continue
                joined = "\n".join(s.text for s in subs)
                if any(m in joined for m in _SKIP_MARKERS):
                    self._processed.add(block_id)
                    continue
                # 驱逐：所有文本块合并为一个占位符（保留首块结构，清空其余）
                subs[0].text = (
                    f"{_EVICTED_MARK} 工具 {tool_name} 的较早结果已归档"
                    f"（原 {total} 字符）。如确需其内容，请用相同参数重新调用该工具。"
                )
                for s in subs[1:]:
                    s.text = ""
                self._processed.add(block_id)
                evicted += 1
                freed += total - len(subs[0].text)
        return evicted, freed
