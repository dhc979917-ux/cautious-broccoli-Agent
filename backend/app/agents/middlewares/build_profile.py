"""BuildProfileMiddleware：按轮解析交付档位，按档位装配质量闭环能力。

为什么用中间件而不是写死在 system_prompt：
system_prompt 在 Agent 构建期就固定了，而档位要看到本轮用户消息才能判定。
框架的 ``on_system_prompt`` 钩子是 transformer 管道（每次模型调用前重写提示词），
正好用来做"按档位条件拼接"——非构建型会话拿到的提示词与改造前逐字一致。

为什么不用 HintBlock 注入（实测踩过）：
规则塞进 assistant 消息确实也在上下文里，但模型会自行判断"这个任务很简单、
不需要验证"然后跳过。助手消息里的提示权威性远低于系统提示词，遵从性不够。

缓存友好性：档位在会话内稳定（关键词判定结果被后续轮沿用、CreatePlan 声明则粘住），
所以系统提示词只在"首次判定为 artifact"那一次变化，之后前缀稳定，
服务端隐式缓存照常命中。

只做三件事：解析档位、按档位拼提示词、按档位激活质量工具组。
不改推理结果、不改工具执行、不吞事件，与其它中间件互不影响。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Callable, TYPE_CHECKING

from agentscope.middleware import MiddlewareBase

from app.agents import delivery_profile as dp
from app.agents.quality_prompts import hints_for, hints_for_interview

if TYPE_CHECKING:
    from agentscope.agent import Agent

logger = logging.getLogger(__name__)

# 质量工具组名（CollectRequirements + VerifyArtifact）。只在 artifact 档激活，
# 非构建型会话看不到组内工具定义，提示词与工具 schema 均为零增量。
QUALITY_TOOL_GROUP = "quality"
FILE_OPS_TOOL_GROUP = "file_ops"
TASK_MGMT_TOOL_GROUP = "task_mgmt"

# 判定档位时回看的上下文消息条数（只看最近的，长会话不做全量扫描）
_USER_SCAN_LIMIT = 12


def _msg_text(msg) -> str:
    """从一条消息里抽出纯文本（content 可能是 str，也可能是 block 列表）。"""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                for key in ("text", "hint"):
                    val = block.get(key)
                    if isinstance(val, str):
                        parts.append(val)
            else:
                for key in ("text", "hint"):
                    val = getattr(block, key, None)
                    if isinstance(val, str):
                        parts.append(val)
        return "\n".join(parts)
    return ""


def _latest_user_text(agent) -> str:
    ctx = getattr(getattr(agent, "state", None), "context", None)
    if not ctx:
        return ""
    for msg in reversed(list(ctx)[-_USER_SCAN_LIMIT:]):
        if getattr(msg, "role", None) == "user":
            text = _msg_text(msg).strip()
            if text:
                return text
    return ""


class BuildProfileMiddleware(MiddlewareBase):
    """交付档位解析 + 按档位装配质量闭环能力。"""

    # ---- 钩子 1：系统提示词按档位拼接 ----
    async def on_system_prompt(self, agent: "Agent", current_prompt: str) -> str:
        """artifact 档追加采集/台账/验证规则；interview 组激活时追加访谈规则；其余零增量。"""
        try:
            self._ensure_profile(agent)
            extra = ""
            if dp.is_artifact(agent):
                extra += hints_for(dp.DeliveryKind.ARTIFACT)
            if self._interview_active(agent):
                extra += hints_for_interview()
            if extra and extra not in current_prompt:
                return current_prompt + extra
            return current_prompt
        except Exception as e:  # 拼装失败退回原提示词，绝不阻断回复
            logger.debug("[build_profile] 系统提示词拼装失败（忽略）: %s", e)
            return current_prompt

    @staticmethod
    def _interview_active(agent) -> bool:
        """interview 工具组是否已激活（由模型经 reset_tools 显式开启，跨轮保留）。"""
        tool_context = getattr(getattr(agent, "state", None), "tool_context", None)
        groups = getattr(tool_context, "activated_groups", None)
        return isinstance(groups, list) and "interview" in groups

    # ---- 钩子 2：每轮解析档位 + 激活质量工具组 ----
    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        try:
            self._ensure_profile(agent)
            if dp.is_artifact(agent):
                self._activate_quality_group(agent)
                self._activate_file_ops_group(agent)
                self._activate_task_mgmt_group(agent)
        except Exception as e:  # 档位判定失败绝不能阻断回复
            logger.debug("[build_profile] 档位解析失败（忽略）: %s", e)

        async for event in next_handler(**input_kwargs):
            yield event

    # ---- 钩子 3：reset_tools 是替换语义，替模型把 quality 组带上 ----
    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        try:
            tool_call = input_kwargs.get("tool_call")
            # 注意：外部执行工具（CollectRequirements）不走 acting 管道——框架直接
            # 发 RequireExternalExecutionEvent 暂停回复，这里拦不到，所以它的指标
            # 由 manager._resolve_requirements 记账，本钩子只管 reset_tools。
            if self._tool_name(tool_call) == "reset_tools" and dp.is_artifact(agent):
                self._inject_group(tool_call, QUALITY_TOOL_GROUP)
                self._inject_group(tool_call, FILE_OPS_TOOL_GROUP)
                self._inject_group(tool_call, TASK_MGMT_TOOL_GROUP)
        except Exception as e:
            logger.debug("[build_profile] on_acting 处理失败（忽略）: %s", e)

        async for item in next_handler(**input_kwargs):
            yield item

    # ---- 内部 ----
    def _ensure_profile(self, agent) -> None:
        """每轮回复解析一次档位（同轮内幂等）。两个钩子都会调，谁先到谁算。"""
        state = getattr(agent, "state", None)
        reply_id = str(getattr(state, "reply_id", "") or "")
        store = getattr(state, "middle_context", {}).setdefault("build_profile", {})
        if store.get("reply_id") == reply_id and store.get("resolved"):
            return
        kind = dp.resolve_for_reply(agent, reply_id, _latest_user_text(agent))
        store["reply_id"] = reply_id
        store["kind"] = kind
        store["resolved"] = True
        if kind == dp.DeliveryKind.ARTIFACT:
            # 激活放在这里而不是只在 on_reasoning：框架可能在推理开始前就调
            # _get_system_prompt（此时 on_reasoning 尚未执行），两处都保证一次，
            # 幂等无副作用。
            self._activate_quality_group(agent)
            self._activate_file_ops_group(agent)
            self._activate_task_mgmt_group(agent)
            logger.info(
                "[build_profile] session=%s reply=%s 档位=artifact（来源=%s）",
                getattr(state, "session_id", ""), reply_id, dp.get_source(agent),
            )

    @staticmethod
    def _activate_quality_group(agent) -> None:
        """把 quality 组补进已激活列表（幂等）。

        在推理前改，本轮模型就能看到组内工具——框架在推理阶段才按
        activated_groups 组装可用工具清单。每轮都要补，因为 reset_tools
        是替换语义，模型一旦调用就会把这里加进去的组清掉。
        """
        tool_context = getattr(getattr(agent, "state", None), "tool_context", None)
        groups = getattr(tool_context, "activated_groups", None)
        if isinstance(groups, list) and QUALITY_TOOL_GROUP not in groups:
            groups.append(QUALITY_TOOL_GROUP)
            logger.debug("[build_profile] 已激活 %s 工具组", QUALITY_TOOL_GROUP)

    @staticmethod
    def _activate_file_ops_group(agent) -> None:
        """把 file_ops 组补进已激活列表（幂等）。

        artifact 档必然需要文件操作（Read/Write/Edit），自动激活可避免
        LLM 忘了 reset_tools 而走试错弯路。与 quality 组同款幂等逻辑。
        """
        tool_context = getattr(getattr(agent, "state", None), "tool_context", None)
        groups = getattr(tool_context, "activated_groups", None)
        if isinstance(groups, list) and FILE_OPS_TOOL_GROUP not in groups:
            groups.append(FILE_OPS_TOOL_GROUP)
            logger.debug("[build_profile] 已激活 %s 工具组", FILE_OPS_TOOL_GROUP)

    @staticmethod
    def _activate_task_mgmt_group(agent) -> None:
        """把 task_mgmt 组补进已激活列表（幂等）。

        artifact 档需要结构化拆解复杂任务（CreatePlan/AdvanceTask），
        自动激活可避免模型因不调 reset_tools 而看不到 plan 工具。
        """
        tool_context = getattr(getattr(agent, "state", None), "tool_context", None)
        groups = getattr(tool_context, "activated_groups", None)
        if isinstance(groups, list) and TASK_MGMT_TOOL_GROUP not in groups:
            groups.append(TASK_MGMT_TOOL_GROUP)
            logger.debug("[build_profile] 已激活 %s 工具组", TASK_MGMT_TOOL_GROUP)

    @staticmethod
    def _tool_name(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("name") or "")
        return str(getattr(tool_call, "name", "") or "")

    @staticmethod
    def _inject_group(tool_call: Any, group: str) -> None:
        """reset_tools 参数里没提到该组时自动补 true（与 FileOpsGuard 同思路）。"""
        raw = tool_call.get("input") if isinstance(tool_call, dict) else getattr(tool_call, "input", None)
        if raw is None:
            return
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                return
        elif isinstance(raw, dict):
            args = raw
        else:
            return
        if not isinstance(args, dict) or group in args:
            return
        args[group] = True
        new_input = json.dumps(args, ensure_ascii=False)
        if isinstance(tool_call, dict):
            tool_call["input"] = new_input
        else:
            try:
                tool_call.input = new_input
            except (AttributeError, TypeError):
                pass
        logger.info("[build_profile] reset_tools 未带 %s，已自动注入", group)
