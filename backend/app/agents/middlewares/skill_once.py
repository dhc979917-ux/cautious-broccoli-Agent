"""Skill 单次访问约束（编码强制，非提示词）。

提供两件事：
- ``OnceSkillViewer``：SkillViewer 子类。同一会话中同一技能只完整返回一次，
  重复调用直接返回简短提示（全文已在上下文），并把技能目录登记到已读集合。
- ``SkillReadBlockMiddleware``：Read 工具中间件。拦截对"已读技能目录"内
  ``*.md`` 说明文档的重复读取（脚本 .py 不拦，确需看实现仍可读）。

已读记录按 session_id 存在模块级注册表（进程内生命周期与 Agent 缓存一致），
用 ``clear_session`` 在会话收尾时清理，防止多人使用时无限增长。
"""

import os
from collections import OrderedDict
from typing import Any, AsyncGenerator, Callable

from agentscope.message import TextBlock
from agentscope.state import AgentState
from agentscope.tool import ToolMiddlewareBase
from agentscope.tool._builtin import SkillViewer
from agentscope.tool._response import ToolChunk

_MAX_SESSIONS = 512
# session_id -> set(viewed skill names)
_viewed_skills: "OrderedDict[str, set]" = OrderedDict()
# session_id -> set(normalized skill dir paths)
_viewed_skill_dirs: "OrderedDict[str, set]" = OrderedDict()


def _touch(reg: "OrderedDict", key: str) -> set:
    if key in reg:
        reg.move_to_end(key)
        return reg[key]
    reg[key] = set()
    while len(reg) > _MAX_SESSIONS:
        reg.popitem(last=False)
    return reg[key]


def _session_key(state: AgentState | None) -> str:
    sid = getattr(state, "session_id", None)
    reply_id = getattr(state, "reply_id", None)
    return f"{sid if sid is not None else '_no_session'}:{reply_id or '_no_reply'}"


def clear_session(session_key: str) -> None:
    prefix = f"{session_key}:"
    for registry in (_viewed_skills, _viewed_skill_dirs):
        for key in list(registry):
            if key.startswith(prefix):
                registry.pop(key, None)


def _is_dir_viewed(session_key: str, norm_path: str) -> bool:
    for d in _viewed_skill_dirs.get(session_key, ()): 
        if norm_path == d or norm_path.startswith(d + os.sep):
            return True
    return False


class OnceSkillViewer(SkillViewer):
    """同一会话同一技能只完整返回一次。"""

    async def call(self, skill: str, _agent_state: AgentState) -> ToolChunk:
        skey = _session_key(_agent_state)
        viewed = _touch(_viewed_skills, skey)
        skills = await self._get_skills_method(_agent_state.tool_context.activated_groups)
        target = skills.get(skill)
        directory = getattr(target, 'dir', '') if target else ''
        view_identity = skill + '@' + directory
        if view_identity in viewed:
            return ToolChunk(content=[TextBlock(
                text=(
                    f"技能 '{skill}' 的完整说明已在本次会话中返回过，"
                    "请直接参考上方上下文里的内容执行（路径、用法都已给出）。"
                    "不要重复查看，也不要用 Read/Glob 探索技能目录。"
                ),
            )])

        result = await super().call(skill=skill, _agent_state=_agent_state)
        viewed.add(view_identity)
        # 登记技能目录，供 Read 拦截使用
        try:
            skills = await self._get_skills_method(
                _agent_state.tool_context.activated_groups,
            )
            target = skills.get(skill)
            if target is not None and getattr(target, "dir", None):
                dirs = _touch(_viewed_skill_dirs, skey)
                dirs.add(os.path.normcase(os.path.normpath(target.dir)))
        except Exception:
            pass
        return result


class SkillReadBlockMiddleware(ToolMiddlewareBase):
    """Read 工具：拦截对已读技能目录内 *.md 说明文档的重复读取。"""

    async def on_tool_call(
        self,
        tool: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[ToolChunk, None]],
    ) -> AsyncGenerator[ToolChunk, None]:
        path = str(
            input_kwargs.get("file_path")
            or input_kwargs.get("path")
            or ""
        )
        state = input_kwargs.get("_agent_state")
        skey = _session_key(state)
        if path and _viewed_skill_dirs.get(skey):
            try:
                norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
            except Exception:
                norm = ""
            if norm and norm.endswith(".md") and _is_dir_viewed(skey, norm):
                yield ToolChunk(content=[TextBlock(
                    text=(
                        "该技能目录下的说明文档已通过 Skill 工具完整返回过，"
                        "完整内容在上方上下文中，请直接使用，无需再次读取。"
                    ),
                )])
                return
        async for chunk in next_handler(**input_kwargs):
            yield chunk
