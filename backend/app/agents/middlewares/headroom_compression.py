"""HeadroomCompressionMiddleware: 全格式上下文压缩（输入侧）。

压缩策略（按内容类型分流）：
- JSON 对象/数组 → headroom SmartCrusher.crush（无损，表格化/去冗余，约 30%）
- 纯文本大块（文件列表/代码/文档）→ 头尾截断 + 完整内容 offload 到 workspace，
  截断提示里附 offload 路径，模型需要时可 Read 回查

输出侧（工具结果进入上下文前）由框架原生 tool_result_limit 截断+offload 承担
（见 ContextConfig.tool_result_limit），本中间件只处理已在上下文中的旧块。

幂等：压缩后块变小或打上已处理标记，后续轮次不会重复压缩。

节流（缓存友好）：原地改写旧块会让本轮 prompt 前缀从改写点起整体失效——实测改写
当轮服务端隐式缓存命中归零，纯追加式增长则能保持 99%。因此压缩不再每轮都做，而是
集中到"工具组变化轮 / 上下文压力轮 / 满 N 轮"三类时机（见 _maybe_compress 与
settings.CONTEXT_COMPRESS_*）。N<=1 即退回每轮都压的旧行为。
"""

import copy
import logging
import asyncio
from typing import AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase
from app.config import settings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agentscope.agent import Agent

try:
    import headroom as _hr
    _HR_OK = True
except Exception:
    _hr = None
    _HR_OK = False

# 触发压缩的最小文本长度（字符）
_MIN_LEN = 800
# 头尾截断保留量（字符）
_KEEP_HEAD = 600
_KEEP_TAIL = 200
# 工具调用参数（ToolCallBlock.input）压缩阈值：超过此长度的 JSON 参数串
# 会被截断（保留结构 + 大值截断 + offload 完整原文）。Write 的 content 参数
# 是最大的上下文膨胀源——一次 Write 14000 字符的 HTML 会永久留在上下文里
# 每轮重发，22 轮 × 4500 token ≈ 10 万 token 仅从一个 Write 调用产生。
_TOOL_CALL_INPUT_THRESHOLD = 2000
# 工具调用参数中单个字符串值的截断保留量
_TOOL_CALL_VALUE_HEAD = 400
_TOOL_CALL_VALUE_TAIL = 200
# 上下文短于此消息数时不压缩（保留最近交互完整性）
# 从 4 降到 3、跳过窗口从 ctx[:-2] 收窄到 ctx[:-1]：只保护最新一条消息，
# 让更多旧块尽早进入压缩，减少跨轮次重发体积。
_MIN_CTX_MSGS = 3
# thinking 块截断保留量（字符）：thinking 仅用于当轮决策，旧轮次保留摘要即可
_THINKING_KEEP = 300
# web 噪音行模式（GitHub 导航/菜单/签名等短行）
_WEB_NOISE_PREFIXES = (
    "Skip to content", "Navigation Menu", "Sign in", "Appearance settings",
    "GitHub Copilot", "Platform", "AI CODE CREATION", "Solutions",
    "Enterprise", "Resources", "Open Source", "Pricing", "Documentation",
    "Search", "Topics", "Explore", "Footer",
)

# Agent 实例上的节流状态属性名（与 _mcp_clients 同款做法，不碰框架字段）
_GATE_ATTR = "_headroom_gate_state"


def _gate_state(agent) -> dict:
    """取（或建）Agent 上的压缩节流计数。挂不上就退化为一次性计数，不影响压缩。"""
    st = getattr(agent, _GATE_ATTR, None)
    if isinstance(st, dict):
        return st
    st = {
        "rounds": 0,             # 本 Agent 已推理多少轮
        "compressions": 0,       # 实际执行过多少次压缩扫描
        "mutated_blocks": 0,     # 累计原地改写了多少个块
        "last_round": None,      # 上次压缩所在轮次
        "last_mutated_round": None,
    }
    try:
        setattr(agent, _GATE_ATTR, st)
    except Exception:
        pass
    return st


def gate_stats(agent) -> dict:
    """节流状态只读快照（观测/测试用）。"""
    return dict(_gate_state(agent))


def _activated_groups(agent) -> tuple:
    """当前激活的工具组快照（顺序无关）。

    工具定义序列化在消息之前，激活组一变，整条 prompt 前缀必然失效——这种轮次做
    压缩是"免费"的，不会额外损失缓存。
    """
    tool_ctx = getattr(getattr(agent, "state", None), "tool_context", None)
    groups = getattr(tool_ctx, "activated_groups", None) or []
    try:
        return tuple(sorted(str(g) for g in groups))
    except Exception:
        return ()


def _estimate_tokens(ctx) -> int:
    """粗估上下文 token 数：字符数 / 3（中英混排经验中值），只用于压力门控。"""
    chars = 0
    for msg in ctx:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            chars += len(getattr(block, "text", "") or "")
            chars += len(getattr(block, "thinking", "") or "")
            tool_input = getattr(block, "input", None)
            if tool_input:
                chars += len(str(tool_input))
            output = getattr(block, "output", None)
            if isinstance(output, list):
                for sub in output:
                    chars += len(getattr(sub, "text", "") or "")
            elif isinstance(output, str):
                chars += len(output)
    return chars // 3


def _context_size(agent) -> int:
    return int(getattr(getattr(agent, "model", None), "context_size", 0) or 0)

class HeadroomCompressionMiddleware(MiddlewareBase):
    """每轮推理前压缩上下文中的旧工具结果块。"""

    def __init__(self) -> None:
        self._smart_crusher = None
        self._initialized = False
        # 已处理过的块 id，避免对压不动的块反复尝试
        self._processed: set = set()

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if not _HR_OK:
            return
        try:
            self._smart_crusher = _hr.SmartCrusher()
        except Exception:
            self._smart_crusher = None

    def _crush_texts_sync(self, texts: list) -> list:
        """CPU 密集同步段：批量 JSON 无损压缩（在线程池里跑，不占事件循环）。
        返回与入参等长的列表：压缩成功为压缩结果，失败/未变化为 None。"""
        out = []
        for t in texts:
            try:
                r = self._smart_crusher.crush(t, lossless_only=True)
                out.append(r.compressed if r.was_modified else None)
            except Exception:
                out.append(None)
        return out

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        self._ensure_init()
        try:
            await self._maybe_compress(agent)
        except Exception as e:  # 压缩失败不阻断推理
            logger.debug("[headroom] compress skipped: %s", e)
        async for event in next_handler(**input_kwargs):
            yield event

    # ------------------------------------------------------------------
    async def _maybe_compress(self, agent: "Agent") -> None:
        """门控：只在必要的轮次原地改写上下文，其余轮次保持前缀稳定吃到缓存。"""
        ctx = agent.state.context
        if len(ctx) < _MIN_CTX_MSGS:
            return
        st = _gate_state(agent)
        st["rounds"] += 1
        round_no = st["rounds"]
        est_tokens = _estimate_tokens(ctx)

        reasons = []
        groups = _activated_groups(agent)
        # 首轮只记基线不算"变化"（Agent 重建后的第一轮同理，代价不可避免）
        if "groups" in st and groups != st["groups"]:
            reasons.append("tools_changed")
        st["groups"] = groups

        pressure_ratio = float(
            getattr(settings, "CONTEXT_COMPRESS_PRESSURE_RATIO", 0) or 0
        )
        context_size = _context_size(agent)
        if pressure_ratio > 0 and context_size > 0:
            if est_tokens > context_size * pressure_ratio:
                reasons.append("pressure")

        every_n = int(getattr(settings, "CONTEXT_COMPRESS_EVERY_N_ROUNDS", 1) or 1)
        last_round = st.get("last_round")
        due = every_n <= 1 or last_round is None or (round_no - last_round) >= every_n
        if not reasons and not due:
            return
        if not reasons:
            reasons.append(f"every_{every_n}_rounds")

        mutated = await self._compress_context(agent)
        st["last_round"] = round_no
        st["compressions"] += 1
        if mutated:
            st["mutated_blocks"] += mutated
            st["last_mutated_round"] = round_no
            # 打点：与 manager 的 [cache] 逐轮日志按 session+round 对齐，
            # 可直接归因"这一轮的缓存未命中是不是压缩造成的"
            logger.info(
                "[headroom] session=%s round=%s 原地改写 %d 个块 reason=%s "
                "ctx_msgs=%d est_tokens=%d/%d",
                getattr(agent.state, "session_id", "?"), round_no, mutated,
                "+".join(reasons), len(ctx), est_tokens, context_size or 0,
            )

    # ------------------------------------------------------------------
    async def _compress_context(self, agent: "Agent") -> int:
        """执行压缩，返回本轮原地改写的块数（0 = 上下文没被动过，前缀仍可用于缓存）。"""
        ctx = agent.state.context
        if len(ctx) < _MIN_CTX_MSGS:
            return 0
        mutated = 0
        # 只保护最近 1 条消息（最新工具结果/模型输出，下一步就要用）
        for msg in ctx[:-1]:
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                # P5: thinking 块截断为摘要 — 旧轮次的 thinking 仅用于当轮决策
                thinking = getattr(block, "thinking", None)
                if isinstance(thinking, str) and len(thinking) > _THINKING_KEEP:
                    bid = getattr(block, "id", None) or id(block)
                    if bid not in self._processed:
                        block.thinking = (
                            thinking[:_THINKING_KEEP]
                            + f"\n…[thinking 已压缩：原 {len(thinking)} 字符]…"
                        )
                        self._processed.add(bid)
                        mutated += 1
                output = getattr(block, "output", None)
                if isinstance(output, list):
                    mutated += await self._compress_tool_block(agent, block)
                # P6: 工具调用参数压缩 — ToolCallBlock.input 里的长 content/arguments
                # 是上下文膨胀的最大单一来源（Write 的完整文件内容）。框架不截断
                # tool_call 的 input，HeadroomCompressionMiddleware 原先也只处理
                # tool_result 的 output，这里补齐对 input 的压缩。
                tool_input = getattr(block, "input", None)
                if isinstance(tool_input, str) and len(tool_input) > _TOOL_CALL_INPUT_THRESHOLD:
                    mutated += await self._compress_tool_call_input(agent, block)
        return mutated

    async def _compress_tool_block(self, agent: "Agent", block) -> int:
        """压缩单个工具结果块，返回 1 表示真的改写了内容，0 表示没动。"""
        subs = [s for s in block.output if getattr(s, "text", None)]
        if not subs:
            return 0
        block_id = getattr(block, "id", None) or id(block)
        if block_id in self._processed:
            return 0

        total = sum(len(s.text) for s in subs)
        if total < _MIN_LEN:
            return 0
        original_copy = copy.deepcopy(block)
        changed = False

        # P1: web 噪音剔除 — 对疑似网页抓取结果，删除导航/菜单等短行噪音
        for s in subs:
            if len(s.text) > _MIN_LEN:
                stripped = self._strip_web_noise(s.text)
                if stripped != s.text:
                    s.text = stripped
                    changed = True

        # 路径 1：JSON 无损压缩（A1/A3：SmartCrusher.crush 是 CPU 密集同步调用，
        # 大 JSON 上可达秒级——丢线程池执行 + 整体超时，超时即放弃本块压缩，
        # 绝不让压缩段饿死事件循环。超时线程在后台自然跑完，无副作用。）
        if self._smart_crusher is not None:
            targets = [s for s in subs if s.text.strip()[:1] in "{["]
            if targets:
                try:
                    crushed = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._crush_texts_sync, [s.text for s in targets]
                        ),
                        timeout=settings.HEADROOM_CRUSH_TIMEOUT_SECONDS,
                    )
                    for s, new_text in zip(targets, crushed):
                        if new_text is not None and len(new_text) < len(s.text):
                            s.text = new_text
                            changed = True
                except asyncio.TimeoutError:
                    logger.warning(
                        "[headroom] crush 超时(>%.0fs)，跳过本块 JSON 压缩",
                        settings.HEADROOM_CRUSH_TIMEOUT_SECONDS,
                    )
                    self._processed.add(block_id)
                    return 1 if changed else 0
                except Exception as e:
                    logger.debug("[headroom] crush 线程失败: %s", e)

        total_after_json = sum(len(s.text) for s in subs)
        if total_after_json < _MIN_LEN:
            self._processed.add(block_id)
            return 1 if changed else 0

        # 路径 2：纯文本头尾截断 + offload 完整内容
        offload_ref = ""
        offloader = getattr(agent, "offloader", None)
        if offloader is not None:
            try:
                offload_ref = await offloader.offload_tool_result(
                    str(agent.state.session_id),
                    original_copy,
                )
            except Exception as e:
                logger.debug("[headroom] offload failed: %s", e)
        if not offload_ref:
            self._processed.add(block_id)
            return 1 if changed else 0

        for s in subs:
            if len(s.text) > _KEEP_HEAD + _KEEP_TAIL + 100:
                orig_len = len(s.text)
                ref_hint = (
                    f"，完整内容已存至 '{offload_ref}'，可用 Read 工具回查"
                    if offload_ref else ""
                )
                s.text = (
                    s.text[:_KEEP_HEAD]
                    + f"\n…[已压缩：原 {orig_len} 字符，中间省略{ref_hint}]…\n"
                    + s.text[-_KEEP_TAIL:]
                )
                changed = True
        self._processed.add(block_id)
        return 1 if changed else 0

    async def _compress_tool_call_input(self, agent: "Agent", block) -> int:
        """压缩工具调用参数（ToolCallBlock.input）中的大字符串值。

        只截断值不截断键：保留 JSON 结构和所有参数名，让模型知道调了什么工具、
        传了哪些参数，只把超长的值（如 Write 的 content）截断为头尾摘要。
        完整原文 offload 到 workspace，需要时可用 Read 回查。
        """
        import json as _json

        raw_input = getattr(block, "input", None)
        if not isinstance(raw_input, str):
            return 0
        block_id = getattr(block, "id", None) or id(block)
        # 用不同前缀避免与 tool_result 的 _processed 集合冲突
        call_bid = f"call:{block_id}"
        if call_bid in self._processed:
            return 0

        try:
            args = _json.loads(raw_input)
        except (ValueError, TypeError):
            # 非 JSON 的纯文本 input（罕见），走头尾截断
            args = None

        changed = False
        if isinstance(args, dict):
            for key, val in args.items():
                if isinstance(val, str) and len(val) > _TOOL_CALL_VALUE_HEAD + _TOOL_CALL_VALUE_TAIL + 100:
                    orig_len = len(val)
                    args[key] = (
                        val[:_TOOL_CALL_VALUE_HEAD]
                        + f"\n…[已压缩：原 {orig_len} 字符"
                        + (f"，完整内容已 offload，可用 Read 回查" if agent else "")
                        + f"]…\n"
                        + val[-_TOOL_CALL_VALUE_TAIL:]
                    )
                    changed = True
        elif len(raw_input) > _TOOL_CALL_INPUT_THRESHOLD:
            # 纯文本 fallback
            orig_len = len(raw_input)
            new_input = (
                raw_input[:_TOOL_CALL_VALUE_HEAD]
                + f"\n…[已压缩：原 {orig_len} 字符]…\n"
                + raw_input[-_TOOL_CALL_VALUE_TAIL:]
            )
            changed = True

        if not changed:
            self._processed.add(call_bid)
            return 0

        # Offload 完整原文
        offload_ref = ""
        offloader = getattr(agent, "offloader", None)
        if offloader is not None:
            try:
                # 构造一个伪 ToolResultBlock 供 offload（接口要求 block 类型）
                offload_block = copy.deepcopy(block)
                offload_ref = await offloader.offload_tool_result(
                    str(agent.state.session_id),
                    offload_block,
                )
            except Exception as e:
                logger.debug("[headroom] tool_call offload failed: %s", e)
        if not offload_ref:
            return 0

        ref_hint = f"，完整内容已存至 '{offload_ref}'，可用 Read 回查" if offload_ref else ""
        # 重新序列化（如果有 offload，在截断的值里补上 ref_hint）
        new_json = _json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else new_input
        if isinstance(args, dict):
            # 在每个被截断的值尾部补 ref_hint
            new_json = new_json.replace(
                "完整内容已 offload，可用 Read 回查",
                f"完整内容已存至 '{offload_ref}'，可用 Read 回查",
            )
        try:
            block.input = new_json
        except (AttributeError, TypeError):
            pass
        self._processed.add(call_bid)
        return 1

    @staticmethod
    def _strip_web_noise(text: str) -> str:
        """删除网页抓取结果中的导航/菜单/签名等噪音行，提高有效信息密度。

        只处理疑似网页内容（含多行且噪音行占比高），纯代码/JSON 不动。
        """
        lines = text.split("\n")
        if len(lines) < 20:
            return text
        noise = 0
        kept = []
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(p) for p in _WEB_NOISE_PREFIXES):
                noise += 1
                continue
            if len(stripped) < 60 and ("http" in stripped or stripped.startswith("[")):
                noise += 1
                continue
            kept.append(line)
        if noise < len(lines) * 0.15:
            return text
        return "\n".join(kept)
