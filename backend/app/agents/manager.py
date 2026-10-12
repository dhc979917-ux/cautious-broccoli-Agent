"""
AgentScope 2.0 集成管理器

职责：
- 按 agent_type 路由到 architectures/ 下对应的架构构建器创建 Agent
- Agent 实例缓存（缓存键含配置指纹，数据库配置变更自动失效）
- 统一的 chat / chat_stream 入口与流式事件翻译（前端协议不变）

架构差异（模型参数、token 预算、ReAct/上下文配置、固定技能）都写在
architectures/ 的各自脚本里，本文件只保留公共链路。
"""

import logging

logger = logging.getLogger(__name__)

from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
from app.models.agent import Agent as AgentModel
from app.models.session_context import SessionContext
from app.agents.architectures import get_builder
from app.agents import pending_input
from app.agents import activity
from app.agents.tool_event_buffer import ToolOutputBuffer
from app.agents.interaction import (
    current_interaction,
    get_channel,
    INTERRUPT,
    NO_CHANNEL_TEXT,
    FORM_NO_CHANNEL_TEXT,
)
from app.agents.middlewares.usage_capture import (
    discard_cache_tokens_delta,
    take_cache_tokens_delta,
    take_strict_call_cache_tokens,
)
from app.config import settings
from typing import Optional
import asyncio
import hashlib
import json
import time
import uuid
from collections import OrderedDict


def _truncate_tool_output(text: str, head: int = 4000, tail: int = 1000) -> str:
    """工具输出截断：保留头尾、省略中间，避免超大输出灌爆 WebSocket 与消息元数据。
    头尾放宽后，markdown 报告/表格类输出能展示得更完整（前端再滚动查看）。"""
    if not text:
        return ""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return text[:head] + f"\n…[中间省略 {omitted} 字符]…\n" + text[-tail:]


def _match_tool_call_id(event_id, active_calls: dict) -> str:
    tool_id = str(event_id or "")
    if tool_id:
        return tool_id
    if len(active_calls) == 1:
        return next(iter(active_calls))
    return ""


def _serialize_bounded_context(payload: dict, byte_limit: int) -> str:
    serialized = json.dumps(payload, ensure_ascii=False)
    while len(serialized.encode("utf-8")) > byte_limit and payload["context"]:
        messages = payload["context"]
        next_turn = next(
            (index for index, message in enumerate(messages[1:], 1) if message.get("role") == "user"),
            len(messages),
        )
        del messages[:next_turn]
        serialized = json.dumps(payload, ensure_ascii=False)
    return serialized


class AgentManager:

    # 一会话一实例：Agent（含记忆/沙箱）在会话生命周期内常驻，跨轮保留上下文。
    # 清理只有三个入口：每日定时清理（main.py 调度 evict_idle_all）、删除会话
    # 即时清理（API 侧调用 evict_session）、容量上限兜底（防进程内存无限增长）。
    MAX_CACHED_AGENTS = 64

    def __init__(self):
        # OrderedDict 实现 LRU：最近使用移末尾，淘汰从头部开始
        self.agents_cache: "OrderedDict[str, object]" = OrderedDict()
        self._last_used: dict = {}
        # cache_key -> session_id：淘汰前查任务运行状态，避免关闭在用实例
        self._key_sessions: dict = {}
        self._invalidated: set[str] = set()
        # 后台资源释放任务的强引用集：防止未完成的 asyncio.Task 被 GC 中途回收
        self._release_tasks: set = set()

    def _session_running(self, cache_key: str) -> bool:
        """缓存项对应会话是否有运行中任务（有则不可淘汰，否则工具连接会被中途关闭）。"""
        sid = self._key_sessions.get(cache_key)
        if sid is None:
            return False
        try:
            from app.services.task_manager import task_manager
            return task_manager.is_running(sid)
        except Exception:
            return False

    async def _evict_agent(self, cache_key: str) -> None:
        """从缓存移除一个 Agent，资源释放拆到后台任务执行。

        拆分原因：沙箱拆除（chown/kill/删除容器）可能被外层任务取消打断——
        CancelledError 是 BaseException，会穿透官方 teardown 里的 except Exception，
        跳过容器回收导致 as_ws_* 泄漏，还会沿调用链冒泡让已生成回复的请求变 500。
        后台释放任务不受请求生命周期取消影响；本方法自身无 await 挂起点，
        在取消流程/finally 中调用也安全。
        """
        agent = self.agents_cache.pop(cache_key, None)
        self._last_used.pop(cache_key, None)
        self._key_sessions.pop(cache_key, None)
        self._invalidated.discard(cache_key)
        if agent is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"[AgentScope] 无事件循环，跳过 Agent 资源释放: {cache_key}")
            return
        task = loop.create_task(self._release_agent_resources(agent, cache_key))
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)

    async def _release_agent_resources(self, agent, cache_key: str = "") -> None:
        """后台释放 Agent 资源（MCP 连接/workspace 沙箱）。

        每个阶段独立容忍一切异常（含 BaseException 层的 CancelledError），
        单步失败不阻断后续步骤，保证沙箱容器尽量被回收。
        """
        try:
            await self._close_mcp_clients(agent)
        except (Exception, asyncio.CancelledError):
            logger.warning(f"[AgentScope] MCP 连接关闭异常（已忽略）: {cache_key}")
        _ws = getattr(agent, "_workspace", None)
        # 先取工作区 id：close() 之后实例状态可能已不可读，而清理预览要靠它拼容器名
        _ws_id = str(getattr(_ws, "workspace_id", "") or "") if _ws is not None else ""
        if _ws is not None:
            # 沙箱复用保护：新 Agent 可能复用了旧 Agent 的 workspace（cached=True），
            # 此时关闭旧 workspace 会把新 Agent 正在使用的沙箱容器杀掉，
            # 导致后续工具调用报 "Session is closed"。检查缓存中是否有其他
            # Agent 仍在使用同一 workspace_id，有则跳过关闭。
            _ws_id_check = str(getattr(_ws, "workspace_id", "") or "")
            _still_in_use = any(
                str(getattr(getattr(_a, "_workspace", None), "workspace_id", "") or "") == _ws_id_check
                for _a in self.agents_cache.values()
                if getattr(_a, "_workspace", None) is not None
            )
            if _still_in_use:
                logger.info(f"[AgentScope] workspace {_ws_id_check} 仍被其他 Agent 使用，跳过关闭: {cache_key}")
            else:
                try:
                    _r = _ws.close()
                    if asyncio.iscoroutine(_r):
                        await _r
                except (Exception, asyncio.CancelledError):
                    logger.warning(f"[AgentScope] workspace close err: {cache_key}")
        if _ws is not None and not _still_in_use:
            # 沙箱随 Agent 一起销毁，它对外发布的服务预览必须同步置为失效，
            # 否则前端会继续显示一个"运行中"的死链。失败一律忽略，不影响资源释放。
            if _ws_id:
                try:
                    from app.agents.sandbox_reaper import CONTAINER_NAME_PREFIX, _expire_previews
                    await _expire_previews(f"{CONTAINER_NAME_PREFIX}{_ws_id}")
                except (Exception, asyncio.CancelledError):
                    logger.debug(f"[AgentScope] 预览失效处理跳过: {cache_key}")

    def find_cache_key_by_workspace_id(self, workspace_id) -> Optional[str]:
        """按 workspace id 反查缓存键（沙箱回收器定位容器归属 Agent 用）。"""
        for key, agent in self.agents_cache.items():
            _ws = getattr(agent, "_workspace", None)
            if _ws is not None and str(getattr(_ws, "workspace_id", "")) == str(workspace_id):
                return key
        return None

    def get_sandbox_container_name(self, session_id: int) -> Optional[str]:
        """按会话反查沙箱容器名；非 docker 沙箱、Agent 已被淘汰时返回 None。

        服务预览在收尾阶段要探测"沙箱里有没有起监听服务"，得先知道探哪个容器。
        Agent 实例被 LRU 淘汰时沙箱已随之销毁，此时返回 None，调用方直接跳过发布。
        """
        from app.agents.workspace import sandbox_container_name

        marker = f"_s{session_id}_"
        for key, agent in self.agents_cache.items():
            if marker not in key:
                continue
            _ws = getattr(agent, "_workspace", None)
            ws_id = str(getattr(_ws, "workspace_id", "") or "") if _ws is not None else ""
            if ws_id:
                return sandbox_container_name(ws_id)
        return None

    async def evict_session(self, session_id: int) -> None:
        """删除会话时即时清理该会话的全部 Agent 实例（含 MCP 连接/沙箱）。"""
        marker = f"_s{session_id}_"
        keys = [k for k in list(self.agents_cache.keys()) if marker in k]
        evicted = 0
        for key in keys:
            if self._session_running(key):
                continue  # 理论上删除前任务已停止；仍在跑则留给后续清理
            await self._evict_agent(key)
            evicted += 1
        if evicted:
            logger.info(f"[AgentScope] 会话 {session_id} 删除，回收 {evicted} 个 Agent 实例")
        # 会话删除即清理 DB 持久化的上下文（跨重启记忆随之作废）
        try:
            from app.database import SessionLocal
            _db = SessionLocal()
            try:
                _db.query(SessionContext).filter(
                    SessionContext.session_id == session_id
                ).delete()
                _db.commit()
            except Exception:
                _db.rollback()
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"[AgentScope] 会话 {session_id} 上下文持久化清理失败（不影响删除）: {e}")

    async def clear_session_awaiting_state(self, session_id: int) -> None:
        """任务终止后检查会话 Agent 是否残留"等待确认/外部执行"的工具调用。

        AgentScope 取消/中断回复时不会清除 awaiting tool calls：残留会让后续
        每次发送都在 _check_incoming_event 直接 ValueError（"Agent is waiting
        for N tool calls..."），整个会话废掉。检测到残留即逐出该 Agent 实例，
        下次任务重建（丢部分上下文，换取会话可用）。
        """
        marker = f"_s{session_id}_"
        for key in list(self.agents_cache.keys()):
            if marker not in key:
                continue
            agent = self.agents_cache.get(key)
            dirty = False
            try:
                state = getattr(agent, "state", None)
                name = getattr(agent, "name", "")
                if state is not None:
                    if hasattr(state, "has_awaiting_tool_calls"):
                        dirty = bool(state.has_awaiting_tool_calls(name))
                    elif hasattr(state, "get_awaiting_tool_calls"):
                        dirty = bool(state.get_awaiting_tool_calls(name))
            except Exception:
                dirty = True  # 检查失败按脏处理：宁可重建，不可毒化会话
            if dirty:
                await self._evict_agent(key)
                logger.info(f"[AgentScope] 会话 {session_id} Agent 残留等待中工具调用，已逐出实例防止毒化后续回复")

    async def evict_idle_all(self) -> None:
        """每日清理：回收所有无运行中任务的 Agent 实例（保留运行中的）。"""
        evicted = 0
        for key in list(self.agents_cache.keys()):
            if self._session_running(key):
                continue
            await self._evict_agent(key)
            evicted += 1
        logger.info(f"[AgentScope] 每日清理：回收 {evicted} 个空闲 Agent 实例")

    # ==================== 上下文数据库持久化 ====================
    # Agent 实例常驻进程内存，但进程重启（uvicorn --reload / 容器重建 / 容量淘汰）会丢内存上下文。
    # 每轮对话结束把 state.context + summary 落库（chat.py worker finally 调 persist_session_context），
    # Agent 重建时按 session_id 灌回（_restore_session_context），实现跨重启上下文不丢。
    MAX_CONTEXT_JSON_BYTES = 5 * 1024 * 1024

    def _restore_session_context(self, agent, session_id: int, db=None) -> None:
        """把 DB 持久化的对话记忆（context + summary）灌回新建的 Agent。
        只恢复记忆；权限规则/工具配置沿用新实例构建结果。任何异常都静默降级为全新上下文。"""
        if not session_id:
            return
        own_db = False
        if db is None:
            from app.database import SessionLocal
            db = SessionLocal()
            own_db = True
        try:
            row = db.query(SessionContext).filter(
                SessionContext.session_id == session_id
            ).first()
            if not row or not row.state_json:
                return
            payload = json.loads(row.state_json)
            msgs = []
            for m in payload.get("context") or []:
                try:
                    msgs.append(Msg.model_validate(m))
                except Exception:
                    logger.warning(f"[AgentScope] 会话 {session_id} 上下文存在无法解析的消息，已跳过")
            summary = payload.get("summary") or ""
            if not msgs and not summary:
                return
            # 新构建的实例 context 必为空（build 时新建 AgentState），直接灌回
            if msgs:
                agent.state.context = msgs
            if summary:
                agent.state.summary = summary
            logger.info(f"[AgentScope] 会话 {session_id} 上下文已恢复：{len(msgs)} 条消息")
        except Exception as e:
            logger.warning(f"[AgentScope] 会话 {session_id} 上下文恢复失败（按全新会话处理）: {e}")
        finally:
            if own_db:
                db.close()

    def persist_session_context(self, session_id: int, db) -> None:
        """每轮对话结束后把会话内最近使用的 Agent 记忆落库（幂等 upsert）。
        db 为调用方（chat worker）的 SQLAlchemy 会话，失败只告警不抛出。"""
        if not session_id:
            return
        marker = f"_s{session_id}_"
        candidates = [
            (self._last_used.get(k, 0.0), k)
            for k in self.agents_cache if marker in k
        ]
        if not candidates:
            return  # 实例已不在缓存（每日清理/淘汰），上一次落库即最新
        candidates.sort(reverse=True)
        agent = self.agents_cache.get(candidates[0][1])
        state = getattr(agent, "state", None)
        if state is None:
            return
        try:
            context = list(getattr(state, "context", None) or [])
            payload = {
                "v": 1,
                "summary": getattr(state, "summary", "") or "",
                "context": [m.model_dump(mode="json") for m in context],
            }
            state_json = _serialize_bounded_context(payload, self.MAX_CONTEXT_JSON_BYTES)
            if len(state_json.encode("utf-8")) > self.MAX_CONTEXT_JSON_BYTES:
                logger.warning(f"[AgentScope] 会话 {session_id} 上下文超限（裁剪后仍 >5MB），跳过落库")
                return
            row = db.query(SessionContext).filter(
                SessionContext.session_id == session_id
            ).first()
            if row is None:
                db.add(SessionContext(session_id=session_id, state_json=state_json))
            else:
                row.state_json = state_json
            db.commit()
        except Exception as e:
            logger.warning(f"[AgentScope] 会话 {session_id} 上下文落库失败（不影响对话）: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    @staticmethod
    def _config_fingerprint(agent_model: AgentModel) -> str:
        """配置指纹：agent_type / 模型 / system_prompt 任一变化都会改变缓存键，
        保证数据库里改配置后立即生效，不必手工清缓存。"""
        raw = "|".join([
            str(agent_model.agent_type or ""),
            str(agent_model.model_config_name or ""),
            str(agent_model.system_prompt or ""),
            json.dumps(agent_model.config or {}, sort_keys=True, default=str),
        ])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]

    async def get_or_create_agent(self, agent_model: AgentModel, user_role: str = "viewer", selected_skills: list = None, user_id: int = None, model: str = None, session_id: int = None, selected_mcps: list = None, db=None):
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED and session_id is not None:
            from app.services.agent_versions import for_session
            if db is None:
                raise RuntimeError("版本化 Agent 构建必须提供会话数据库")
            frozen_agent = for_session(db, agent_model, session_id)
            if getattr(agent_model, '_skill_runtime_plan', None) is None:
                if (agent_model.config or {}).get('_scheduled_skill_versions'):
                    frozen_agent.config['_scheduled_skill_versions'] = agent_model.config['_scheduled_skill_versions']
                agent_model = frozen_agent
            elif agent_model.config.get('_agent_version_id') != frozen_agent.config.get('_agent_version_id'):
                raise RuntimeError('本轮 Agent 版本与会话固定版本不一致')
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from app.services.skill_runtime import prepare_agent
            if getattr(agent_model, '_skill_runtime_plan', None) is None:
                agent_model = prepare_agent(db, agent_model, selected_skills, user_role, user_id)
        # selected_skills 语义：None=不限制（全部角色可用技能）；非 None(含空列表)=白名单，空列表=不启用任何技能
        if selected_skills is None:
            skills_key = "all"
        else:
            # 空列表→空串，必须与 "all" 区分，否则会命中"全部技能"的缓存
            skills_key = "_".join(sorted(selected_skills))
        # MCP selection cache key: None = all bound MCPs; list = subset (empty = none)
        if selected_mcps is None:
            mcp_key = "all"
        else:
            mcp_key = "_".join(str(int(i)) for i in sorted(int(i) for i in selected_mcps))
        fingerprint = self._config_fingerprint(agent_model)
        # model=本次会话用户选择的模型（None=用 Agent 的 DB 配置模型），计入缓存键，不同模型各自独立实例
        # session_id 计入缓存键：每个会话独立 Agent 实例，进程内记忆不再跨会话共享
        cache_key = f"{agent_model.id}_{user_role}_{skills_key}_{mcp_key}_u{user_id or 0}_s{session_id or 0}_m{model or ''}_{fingerprint}"

        if cache_key in self.agents_cache and (
            cache_key not in self._invalidated or self._session_running(cache_key)
        ):
            self.agents_cache.move_to_end(cache_key)
            self._last_used[cache_key] = time.time()
            return self.agents_cache[cache_key]
        if cache_key in self._invalidated:
            await self._evict_agent(cache_key)

        # 按 agent_type 路由到对应架构构建器（architectures/）；模型优先用本次所选，否则用 DB 的 model_config_name
        builder = get_builder(agent_model.agent_type)
        agent = await builder.build(
            agent_model,
            user_role=user_role,
            selected_skills=selected_skills,
            selected_mcp_ids=selected_mcps,
            user_id=user_id,
            model_override=model,
            session_id=session_id,
            db=db,
        )

        # 跨重启上下文恢复：进程重启/缓存淘汰后重建实例时，从 DB 灌回历史对话记忆
        self._restore_session_context(agent, session_id, db)

        # DB 配置变更后，同一 Agent + 同一模型下的旧版本缓存不会再命中，主动清掉防止内存膨胀
        # （只清当前模型的前缀，其他模型的实例保留，避免用户切换模型导致反复重建）
        stale_prefix = f"{agent_model.id}_{user_role}_{skills_key}_{mcp_key}_u{user_id or 0}_s{session_id or 0}_m{model or ''}_"
        for stale_key in [k for k in self.agents_cache if k.startswith(stale_prefix)]:
            if self._session_running(stale_key):
                continue  # 旧实例仍被运行中任务使用，延后回收
            await self._evict_agent(stale_key)

        self.agents_cache[cache_key] = agent
        agent._cache_key = cache_key
        self._last_used[cache_key] = time.time()
        self._key_sessions[cache_key] = session_id
        # 容量上限：超出时淘汰最久未用的实例（头部）
        if len(self.agents_cache) > self.MAX_CACHED_AGENTS:
            for key in list(self.agents_cache.keys()):
                if len(self.agents_cache) <= self.MAX_CACHED_AGENTS:
                    break
                if self._session_running(key):
                    continue  # 跳过有运行中任务的实例，避免中途关闭
                logger.info(f"[AgentScope] 缓存容量淘汰: {key}")
                await self._evict_agent(key)
        logger.info(f"[AgentScope] 创建Agent: {agent_model.name} (架构={agent_model.agent_type}, 模型={model or agent_model.model_config_name})")
        return agent

    async def chat(self, agent_model: AgentModel, user_message: str, user_role: str = "viewer", selected_skills: list = None, user_id: int = None, model: str = None, session_id: int = None, selected_mcps: list = None, structured_schema: dict = None, db=None) -> str:
        """非流式对话：内部复用 chat_stream，只收集文本回复。
        注意：AgentScope 2.0 的 agent.reply 是异步方法，不能同步调用，
        因此这里通过消费 chat_stream 的 chunk 事件来拼装最终回复。
        """
        reply_text = ""
        async for event in self.chat_stream(
            agent_model, user_message, user_role=user_role,
            selected_skills=selected_skills, user_id=user_id, model=model,
            session_id=session_id,
            selected_mcps=selected_mcps,
            structured_schema=structured_schema,
            db=db,
        ):
            if event.get("event") == "chunk":
                reply_text += event.get("content", "")
        return reply_text

    def _parse_tool_name(self, raw_name: str, raw_args: str) -> str:
        """从Skill包装器中提取真实的技能名称"""
        if raw_name == "Skill" and raw_args:
            try:
                args_obj = json.loads(raw_args)
                real_skill = args_obj.get("skill", "")
                if real_skill:
                    return real_skill
            except (json.JSONDecodeError, AttributeError):
                pass
        return raw_name

    def _parse_tool_args_display(self, raw_name: str, raw_args: str) -> tuple:
        """解析工具参数为（展示文本, 一句话描述）。

        PowerShell/Bash：显示 JSON 解码后的真实命令（恢复换行），description 提到头部；
        Read/Write/Edit/Glob/Grep：只显示路径/模式等关键信息（Write/Edit 的大段内容
        不展示，只留字符数）；其余工具原样展示。
        """
        if raw_name == "Skill" and raw_args:
            try:
                args_obj = json.loads(raw_args)
                # 去掉 "skill" 键，只保留真正传给技能的参数
                useful = {k: v for k, v in args_obj.items() if k != "skill"}
                if useful:
                    return json.dumps(useful, ensure_ascii=False, indent=2), ""
                else:
                    return "", ""  # 没有额外参数就不显示
            except (json.JSONDecodeError, AttributeError):
                pass
            return raw_args or "", ""

        try:
            args_obj = json.loads(raw_args) if raw_args else {}
        except (json.JSONDecodeError, TypeError):
            return raw_args or "", ""
        if not isinstance(args_obj, dict):
            return raw_args or "", ""

        if raw_name in ("PowerShell", "Bash"):
            return str(args_obj.get("command") or ""), str(args_obj.get("description") or "")
        if raw_name == "Read":
            extra = ""
            if args_obj.get("offset") is not None or args_obj.get("limit") is not None:
                extra = f"（offset={args_obj.get('offset')}, limit={args_obj.get('limit')}）"
            return f"{args_obj.get('file_path', '')}{extra}", "读取文件"
        if raw_name == "Write":
            content = args_obj.get("content") or ""
            return f"{args_obj.get('file_path', '')}（写入 {len(content)} 字符）", "写入文件"
        if raw_name == "Edit":
            old = args_obj.get("old_string") or ""
            new = args_obj.get("new_string") or ""
            suffix = "（全部替换）" if args_obj.get("replace_all") else ""
            return f"{args_obj.get('file_path', '')}（{len(old)} 字符 → {len(new)} 字符）{suffix}", "编辑文件"
        if raw_name == "Glob":
            path = args_obj.get("path") or ""
            return f"{args_obj.get('pattern', '')}" + (f"（目录: {path}）" if path else ""), "查找文件"
        if raw_name == "Grep":
            path = args_obj.get("path") or ""
            return f"{args_obj.get('pattern', '')}" + (f"（目录: {path}）" if path else ""), "搜索内容"
        return raw_args or "", ""

    async def chat_stream(self, agent_model: AgentModel, user_message: str, user_role: str = "viewer", selected_skills: list = None, user_id: int = None, model: str = None, session_id: int = None, selected_mcps: list = None, structured_schema: dict = None, db=None):
        """
        异步流式对话，产出结构化事件（官方事件形状 + 外部执行恢复循环）。

        核心流程（AgentScope 2.0.5 官方机制）：
        - reply_stream(inputs) 开始回复；模型调用外部执行工具（AskUserChoice）时
          产出 RequireExternalExecutionEvent 暂停回复（无 ReplyEndEvent）；
        - 本方法把问题经 InteractionChannel 推给前端等待用户选择，然后组装
          ExternalExecutionResultEvent 再次调用 reply_stream 恢复；
        - 用户在暂停态停止时（INTERRUPT 哨兵），用 UserInterruptEvent 干净收尾；
        - structured_schema 传 JSON Schema dict 时，回复结束时 structured_output
          随 done 事件回传。

        事件类型：
        - thinking / chunk / tool_call / tool_result / queued_delivered：同前
        - require_external_execution: {"event","reply_id","tool_calls":[{id,name,input}]}
        - tasks: {"event","tasks":[{id,subject,description,state}]}（官方 Task 工具快照）
        - done: {"event","token_usage","tool_call_count","tool_error_count","model_call_rounds","structured_output"}
        """
        agent = await self.get_or_create_agent(agent_model, user_role=user_role, selected_skills=selected_skills, user_id=user_id, model=model, session_id=session_id, selected_mcps=selected_mcps, db=db)
        # Agent 实例按会话常驻（跨轮保留上下文），不随单轮结束回收；
        # 沙箱/连接的清理由每日清理、删除会话、容量兜底三个入口负责
        async for event in self._chat_stream_events(
            agent, user_message, session_id=session_id, structured_schema=structured_schema,
        ):
            yield event
        # 正常收尾即落库：REST 同步链路（chat → chat_stream）没有 worker finally 兜底，
        # 在此统一持久化；WS 链路 worker finally 还会再落一次（幂等 upsert，覆盖取消/异常场景）
        try:
            if db is not None:
                self.persist_session_context(session_id, db)
            else:
                from app.database import SessionLocal
                _db_p = SessionLocal()
                try:
                    self.persist_session_context(session_id, _db_p)
                finally:
                    _db_p.close()
        except Exception as e:
            logger.warning(f"[AgentScope] 会话 {session_id} 上下文落库失败（不影响回复）: {e}")

    async def _chat_stream_events(self, agent, user_message: str, session_id: int = None, structured_schema: dict = None):
        """回复事件流外壳（D7）：登记会话活跃阶段，保证正常结束/异常/取消
        任何路径都清理活动登记表，lag 告警才能准确归因。"""
        if session_id is not None:
            activity.set_active(session_id, "react:start")
        try:
            from opentelemetry import trace
            from opentelemetry.trace import Status, StatusCode
            with trace.get_tracer(__name__).start_as_current_span('agent_run') as span:
                for key, value in getattr(agent, '_governance_trace', {}).items():
                    span.set_attribute(key, value)
                try:
                    async for event in self._chat_stream_core(
                        agent, user_message, session_id=session_id, structured_schema=structured_schema,
                    ):
                        if event.get('event') == 'error':
                            span.set_status(Status(StatusCode.ERROR))
                        yield event
                except Exception:
                    span.set_status(Status(StatusCode.ERROR))
                    raise
        finally:
            if session_id is not None:
                activity.clear_active(session_id)

    async def _chat_stream_core(self, agent, user_message: str, session_id: int = None, structured_schema: dict = None):
        """真正的回复事件流（原 chat_stream 主体）。"""
        channel = current_interaction.get()
        if channel is None and session_id is not None:
            # contextvar 在当前上下文不可见时（任务切换/上下文丢失），按会话从注册表兜底取通道，
            # 否则 _resolve_external_execution 走 NO_CHANNEL 分支、不登记 future，用户点卡片会"未找到外部执行请求"
            channel = get_channel(session_id)
            logger.info(f"[chat_stream] contextvar 交互通道为空，按 session_id={session_id} 从注册表兜底取通道: {'命中' if channel else '仍未命中'}")

        msg = Msg(name="user", content=[TextBlock(text=user_message)], role="user")

        # 状态追踪（跨恢复调用共享）
        model_call_round = 0           # 第几轮模型调用
        current_model_name = ""        # 本轮模型名（缓存打点用，取自 ModelCallStartEvent）
        has_tool_calls = False         # 是否调用过工具
        tool_calls_pending = {}        # tool_call_id -> {"name": str, "args": str}
        tool_display_names = {}        # tool_call_id -> 展示名（供工具输出事件回配）
        tool_result_buffers = {}       # tool_call_id -> 有界首尾缓冲，保留真实的输出尾部
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_tokens = 0
        snapshot_cache_tokens = 0
        snapshot_cache_known = True
        tool_error_count = 0           # 失败的工具调用次数（可验证性：失败可见、可聚合）
        actual_tool_count = 0          # 实际发起的工具调用次数
        last_tasks_snapshot = None     # 官方任务列表快照（变化时产出 tasks 事件）

        import time as _t
        _round_start = _t.monotonic()  # ReAct 循环起点：轮数/耗时告警与结束摘要
        # C6：单轮硬时长上限——ReAct 循环（含工具执行）超过即强制截止，
        # 防止单个任务无限占用事件循环拖垮整个后端
        _turn_deadline = _round_start + settings.AGENT_TURN_TIMEOUT_SECONDS
        turn_timed_out = False
        current_input = msg
        schema_for_call = structured_schema  # 恢复调用时官方忽略且告警，只在首次传
        while True:
            pending_resume = None          # RequireExternalExecutionEvent：回复暂停，等待外部执行
            pending_confirmation = None
            failed_confirmations = set()
            async for event in self._reply_stream_with_deadline(
                agent.reply_stream(
                    inputs=current_input,
                    structured_schema=schema_for_call,
                    yield_final_msg=True,
                ),
                _turn_deadline,
                session_id,
            ):
                schema_for_call = None
                # C6：超时标记（wrapper 已取消回复任务）→ 跳出收尾
                if isinstance(event, dict) and event.get("__turn_timeout__"):
                    turn_timed_out = True
                    break
                if isinstance(event, Msg):
                    # yield_final_msg=True 时的最终消息（暂停态为占位消息），不需要单独转发
                    continue
                event_type = type(event).__name__

                # ========== 模型调用开始 ==========
                if event_type == "ModelCallStartEvent":
                    model_call_round += 1
                    current_model_name = getattr(event, 'model_name', '') or current_model_name
                    # D7：登记当前阶段，供事件循环 lag 告警归因
                    if session_id is not None:
                        activity.set_active(session_id, f"react:model#{model_call_round}")
                    # 长任务可观测性：每 10 轮打点 + 50 轮告警（max_iters 默认未限，
                    # 防止不收敛的任务无声拖垮事件循环 / 健康探针）
                    if model_call_round % 10 == 0:
                        logger.info(
                            f"[react] session={session_id} round={model_call_round} "
                            f"tools={actual_tool_count} "
                            f"elapsed={_t.monotonic() - _round_start:.1f}s"
                        )
                    if model_call_round == 50:
                        logger.warning(
                            f"[react] session={session_id} 已达 50 轮模型调用，"
                            "疑似不收敛，持续运行中（未设上限）"
                        )
                    # 排队输入延时投递：从第 2 轮模型调用起（此时上一轮工具结果已回到上下文，
                    # 注入的 user 消息不会卡在 tool_call 与 tool_result 之间），把运行期间
                    # 用户排队的内容 observe 进上下文，本轮模型调用即可看到。
                    # 时机关键：ModelCallStartEvent 在框架 _prepare_model_input 之前 yield，
                    # 这里注入正好赶在本轮模型输入组装之前。
                    if model_call_round >= 2 and session_id is not None:
                        queued_items = pending_input.drain(session_id)
                        if queued_items:
                            inject_text = pending_input.compose_injection_text(queued_items)
                            await agent.observe(
                                Msg(name="user", content=[TextBlock(text=inject_text)], role="user")
                            )
                            yield {
                                "event": "queued_delivered",
                                "items": [
                                    {
                                        "content": it["content"],
                                        "attachments": pending_input.item_attachments(it),
                                    }
                                    for it in queued_items
                                ],
                            }

                # ========== 工具调用开始 ==========
                elif event_type == "ToolCallStartEvent":
                    tool_name = getattr(event, 'tool_call_name', '未知工具')
                    tool_id = str(getattr(event, 'tool_call_id', '') or '')
                    if not tool_id:
                        tool_id = f"unidentified-{uuid.uuid4().hex}"
                    tool_calls_pending[tool_id] = {"name": tool_name, "args": ""}

                # ========== 工具参数流式传入 ==========
                elif event_type == "ToolCallDeltaEvent":
                    delta = getattr(event, 'delta', '')
                    if delta:
                        # 优先按事件携带的 tool_call_id 归位（并行工具调用时参数不会串）；
                        # 事件没有 id 时退化为追加到最近一个 pending 调用
                        tid = _match_tool_call_id(getattr(event, 'tool_call_id', None), tool_calls_pending)
                        if tid in tool_calls_pending:
                            tool_calls_pending[tid]["args"] += delta

                # ========== 工具调用结束 → 这里才发给前端 ==========
                elif event_type == "ToolCallEndEvent":
                    tool_id = _match_tool_call_id(getattr(event, 'tool_call_id', None), tool_calls_pending)
                    tc = tool_calls_pending.pop(tool_id, None)
                    if tc is None:
                        logger.warning("[tools] 无法按 ID 配对工具调用结束事件: session=%s", session_id)
                        continue

                    # 解析真实工具名和参数
                    real_name = self._parse_tool_name(tc["name"], tc["args"])
                    display_args, arg_summary = self._parse_tool_args_display(tc["name"], tc["args"])
                    tool_display_names[tool_id] = real_name
                    # D7：工具执行阶段登记（工具执行是长阻塞高发段）
                    if session_id is not None:
                        activity.set_active(session_id, f"tool:{real_name}")

                    has_tool_calls = True
                    actual_tool_count += 1

                    # 展示截断放在去重之后：不影响去重判定，只防止超长参数撑爆前端/元数据
                    if len(display_args) > 2000:
                        display_args = display_args[:2000] + "\n…[参数过长，已截断]…"

                    # 发送工具调用事件
                    yield {
                        "event": "tool_call",
                        "tool_call_id": tool_id,
                        "name": real_name,
                        "args": display_args,
                        "description": arg_summary,
                        "round": model_call_round,
                        "status": "calling"
                    }

                # ========== 文本块开始/结束 → 忽略 ==========
                elif event_type in ("TextBlockStartEvent", "TextBlockEndEvent",
                                    "ThinkingBlockStartEvent", "ThinkingBlockEndEvent"):
                    pass

                # ========== 文本内容流式生成 ==========
                elif event_type == "TextBlockDeltaEvent":
                    delta_text = getattr(event, 'delta', '')
                    if delta_text:
                        # 流式推送回复文本：每个 delta 立即产出 chunk
                        yield {"event": "chunk", "content": delta_text}

                # ========== 推理内容流式生成（真实思考过程） ==========
                elif event_type == "ThinkingBlockDeltaEvent":
                    delta_text = getattr(event, 'delta', '')
                    if delta_text:
                        yield {"event": "thinking", "content": delta_text}

                # ========== 模型调用结束 ==========
                elif event_type == "ModelCallEndEvent":
                    input_t = getattr(event, 'input_tokens', 0) or 0
                    output_t = getattr(event, 'output_tokens', 0) or 0
                    total_input_tokens += input_t
                    total_output_tokens += output_t
                    # 框架的 ModelCallEndEvent 没有 cache 字段（getattr 恒为 0），
                    # 真实命中数由 UsageCaptureMiddleware 在 on_model_call 处记录：
                    # 走"累计增量"口径而非按 reply_id 的 FIFO 队列——HITL 暂停-恢复
                    # 或事件生产/消费错位时 FIFO 会整轮记丢（实测偏差近 20 个百分点），
                    # 增量口径与轮次顺序无关，不丢不重。事件里若将来带上 cache 值
                    # 则优先用事件值，并把中间件队列里同轮的记录丢弃防双计。
                    event_cache_t = getattr(event, 'cache_input_tokens', 0) or 0
                    call_cache_t = take_strict_call_cache_tokens(
                        agent, str(getattr(event, 'reply_id', '') or '')
                    )
                    if event_cache_t:
                        call_cache_t = event_cache_t
                    if call_cache_t is None:
                        snapshot_cache_known = False
                    else:
                        snapshot_cache_tokens += call_cache_t
                    if event_cache_t:
                        discard_cache_tokens_delta(agent)
                        cache_t = event_cache_t
                    else:
                        cache_t = take_cache_tokens_delta(agent)
                    total_cache_tokens += cache_t
                    yield {
                        'event': 'usage',
                        'model_call_rounds': model_call_round,
                        'token_usage': {
                            'input_tokens': total_input_tokens,
                            'output_tokens': total_output_tokens,
                            'cache_input_tokens': snapshot_cache_tokens if snapshot_cache_known else None,
                        },
                    }
                    # 逐轮缓存打点：定位"哪一轮把 prompt 前缀打掉了"
                    # （配合 [headroom] 的压缩打点与 traces 里的工具定义变化归因）
                    if input_t > 0:
                        logger.info(
                            f"[cache] session={session_id} round={model_call_round} "
                            f"model={current_model_name or '?'} input={input_t} "
                            f"output={output_t} cache={cache_t} "
                            f"rate={cache_t / input_t * 100:.1f}%"
                        )

                    # 如果这一轮有工具调用，把状态改成done

                # ========== 系统提示 → 忽略 ==========
                elif event_type in ("HintBlockEvent", "ReplyStartEvent"):
                    pass

                # ========== 达到最大推理轮数 → 显式提示 ==========
                elif event_type == "ExceedMaxItersEvent":
                    yield {
                        "event": "chunk",
                        "content": "\n\n⚠️ **已达到最大推理轮数，任务被强制截止**：剩余步骤未完成。可以重试，或把任务拆分成多步分次发送。",
                    }

                # ========== 工具执行输出 → 收集并按调用回配（前端"实际操作"小框展示）==========
                elif event_type == "ToolResultTextDeltaEvent":
                    tid = _match_tool_call_id(getattr(event, 'tool_call_id', ''), tool_display_names)
                    delta = getattr(event, 'delta', '')
                    if delta and tid in tool_display_names:
                        tool_result_buffers.setdefault(tid, ToolOutputBuffer()).append(delta)

                elif event_type == "ToolResultEndEvent":
                    tid = _match_tool_call_id(getattr(event, 'tool_call_id', ''), tool_display_names)
                    if tid not in tool_display_names:
                        logger.warning("[tools] 无法按 ID 配对工具执行结果: session=%s", session_id)
                        continue
                    output = tool_result_buffers.pop(tid, ToolOutputBuffer()).render()
                    # 工具执行状态（success/error/...）：传播给前端与落库，失败不再埋在输出文本里
                    raw_state = getattr(event, 'state', None)
                    result_state = str(getattr(raw_state, 'value', raw_state) or 'success')
                    if result_state == 'error':
                        tool_error_count += 1
                    yield {
                        "event": "tool_result",
                        "tool_call_id": tid,
                        "name": tool_display_names.pop(tid, ""),
                        "output": output,
                        "state": result_state,
                    }
                    # 官方 Task 工具快照：任务列表变化时产出 tasks 事件（前端计划面板渲染）
                    tasks_ctx = getattr(getattr(agent, "state", None), "tasks_context", None)
                    if tasks_ctx is not None:
                        snap = [
                            {"id": t.id, "subject": t.subject, "description": t.description, "state": t.state}
                            for t in tasks_ctx.tasks
                        ]
                        if snap and snap != last_tasks_snapshot:
                            last_tasks_snapshot = snap
                            yield {"event": "tasks", "tasks": snap}
                    # D7：工具执行结束，回到推理阶段
                    if session_id is not None:
                        activity.set_active(session_id, "react")

                # ========== 权限确认请求 → 用户明确允许才继续执行 ==========
                elif event_type == "RequireUserConfirmEvent":
                    pending_confirmation = event
                    if settings.GOVERNANCE_TOOL_POLICY_ENABLED:
                        from app.agents.tool_policy import argument_summary
                    if channel is not None:
                        for tool_call in getattr(event, "tool_calls", []):
                            binding = getattr(agent, "_governance_tools", {}).get(tool_call.name)
                            if settings.GOVERNANCE_TOOL_POLICY_ENABLED and binding:
                                from app.agents.tool_policy import begin_confirmation
                                try:
                                    begin_confirmation(channel.user_id, session_id,
                                        agent._governance_agent_id, binding[0], binding[1],
                                        str(tool_call.id), tool_call.input)
                                except Exception:
                                    failed_confirmations.add(str(tool_call.id))
                                    logger.error("MCP Tool 审批登记不可用，已拒绝本次调用")
                            channel.announce(str(tool_call.id), "confirm")
                    yield {
                        "event": "require_external_execution",
                        "reply_id": getattr(event, "reply_id", ""),
                        "timeout_seconds": min(settings.ASK_USER_TIMEOUT_SECONDS,
                                               60 if settings.GOVERNANCE_TOOL_POLICY_ENABLED else 300),
                        "tool_calls": [
                            {
                                "id": str(tc.id),
                                "name": "AskUserChoice",
                                "input": json.dumps({
                                    "question": f"是否允许执行工具 {tc.name}？" + (
                                        " " + argument_summary(tc.input)
                                        if settings.GOVERNANCE_TOOL_POLICY_ENABLED and tc.name in getattr(agent, "_governance_tools", {})
                                        else ""
                                    ),
                                    "options": [{"label": "允许执行"}, {"label": "拒绝执行"}],
                                }, ensure_ascii=False),
                            }
                            for tc in getattr(event, "tool_calls", [])
                        ],
                    }

                # ========== 外部执行请求（官方 HITL）→ 记录暂停，恢复循环处理 ==========
                elif event_type == "RequireExternalExecutionEvent":
                    pending_resume = event
                    if channel is not None:
                        for tool_call in getattr(event, "tool_calls", []):
                            channel.announce(str(tool_call.id), "intake" if tool_call.name == "CollectRequirements" else "choice")
                    yield {
                        "event": "require_external_execution",
                        "reply_id": getattr(event, "reply_id", ""),
                        # 前端倒计时用：与 ask_external 的实际等待上限一致
                        "timeout_seconds": settings.ASK_USER_TIMEOUT_SECONDS,
                        "tool_calls": [
                            {"id": str(tc.id), "name": tc.name, "input": tc.input}
                            for tc in getattr(event, "tool_calls", [])
                        ],
                    }

                # ========== 回复结束 ==========
                elif event_type == "ReplyEndEvent":
                    # 末轮竞态兜底：最后一次模型调用的 usage 可能略晚于
                    # ModelCallEndEvent 落账，收尾时把剩余增量取净
                    _tail_cache_t = take_cache_tokens_delta(agent)
                    if _tail_cache_t:
                        total_cache_tokens += _tail_cache_t
                    done_payload = {
                        "event": "done",
                        "token_usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                            "cache_input_tokens": total_cache_tokens,
                            "cache_rate": round(total_cache_tokens / total_input_tokens * 100, 1) if total_input_tokens > 0 else 0,
                        },
                        "tool_call_count": actual_tool_count,
                        "tool_error_count": tool_error_count,
                        "model_call_rounds": model_call_round,
                    }
                    # 结构化输出：最终消息在本事件之后才 yield，从状态里读取
                    structured_output = getattr(
                        getattr(agent.state, "reply_context", None), "structured_output", None
                    )
                    if structured_output is not None:
                        done_payload["structured_output"] = structured_output
                    # 质量闭环指标（档位 + 采集/验证计数）随 done 事件上报，
                    # 由 chat.py 落进消息 metadata 供监控聚合，不动表结构
                    try:
                        from app.agents import quality_metrics as _qm
                        _quality = _qm.snapshot(agent)
                        if _quality:
                            done_payload["quality"] = _quality
                    except Exception:
                        pass
                    yield done_payload

            # C6：单轮超时收尾——警告 + done 事件，跳出恢复循环
            if turn_timed_out:
                logger.warning(
                    f"[react] session={session_id} 单轮执行超过 "
                    f"{settings.AGENT_TURN_TIMEOUT_SECONDS}s，强制截止"
                )
                # 超时被取消前已完成的模型调用可能还有未取走的缓存增量，一并入账
                _tail_cache_t = take_cache_tokens_delta(agent)
                if _tail_cache_t:
                    total_cache_tokens += _tail_cache_t
                yield {
                    "event": "chunk",
                    "content": (
                        f"\n\n⚠️ **单轮执行时长超过 {settings.AGENT_TURN_TIMEOUT_SECONDS // 60} 分钟，任务被强制截止**："
                        "已产出的内容保留。建议把任务拆小分次发送，或避免长时间无输出的命令。"
                    ),
                }
                yield {
                    "event": "done",
                    "token_usage": {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "cache_input_tokens": total_cache_tokens,
                        "cache_rate": round(total_cache_tokens / total_input_tokens * 100, 1) if total_input_tokens > 0 else 0,
                    },
                    "tool_call_count": actual_tool_count,
                    "tool_error_count": tool_error_count,
                    "model_call_rounds": model_call_round,
                    "timed_out": True,
                }
                break

            # 本次 reply_stream 结束：暂停在外部执行 → 等用户答复后恢复；
            # 权限确认 → 用户批准后继续；否则整体结束
            if pending_resume is not None:
                _wait_start = _t.monotonic()
                resume_input = await self._resolve_external_execution(channel, pending_resume, agent)
                # 等用户答复的时间不计入单轮时限（交互等待不是执行阻塞）
                _turn_deadline += _t.monotonic() - _wait_start
                if resume_input is None:
                    break
                current_input = resume_input
                continue
            if pending_confirmation is not None:
                wait_started = _t.monotonic()
                confirm_results = []
                for tool_call in getattr(pending_confirmation, "tool_calls", []):
                    answer = await channel.ask_confirmation(
                        str(tool_call.id), getattr(pending_confirmation, "reply_id", ""), tool_call.name,
                    ) if channel is not None and str(tool_call.id) not in failed_confirmations else None
                    if answer is INTERRUPT:
                        current_input = UserInterruptEvent(reply_id=getattr(pending_confirmation, "reply_id", ""))
                        break
                    if settings.GOVERNANCE_TOOL_POLICY_ENABLED and (
                        tool_call.name in getattr(agent, "_governance_tools", {})
                    ) and str(tool_call.id) not in failed_confirmations:
                        from app.agents.tool_policy import resolve_confirmation
                        try:
                            resolve_confirmation(session_id, str(tool_call.id), answer == "允许执行")
                        except Exception:
                            logger.error("MCP Tool 审批结果无法审计，已拒绝执行")
                            answer = None
                    confirm_results.append(ConfirmResult(
                        confirmed=answer == "允许执行", tool_call=tool_call,
                    ))
                else:
                    current_input = UserConfirmResultEvent(
                        reply_id=getattr(pending_confirmation, "reply_id", ""),
                        confirm_results=confirm_results,
                    )
                _turn_deadline += _t.monotonic() - wait_started
                continue
            break

        # 任务结束摘要：轮数/工具数/耗时，便于排查"长任务拖垮后端"类问题
        _elapsed_total = _t.monotonic() - _round_start
        logger.info(
            f"[react] session={session_id} 结束: rounds={model_call_round} "
            f"tools={actual_tool_count} tool_errors={tool_error_count} "
            f"elapsed={_elapsed_total:.1f}s"
        )

    async def _reply_stream_with_deadline(self, agen, deadline: float, session_id=None):
        """C6：给 reply_stream 套硬时长上限的包装器。

        事件在后台生产者任务里消费、经队列转发；到达 deadline 时取消生产者
        （语义等同用户主动停止：AgentScope 吞掉 CancelledError 优雅收尾），
        并 yield 一个 {"__turn_timeout__": True} 标记事件供调用方收尾。
        消费侧自身被取消时（用户停止）同样在 finally 里取消生产者，不泄漏任务。
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=2048)
        _sentinel = object()
        producer_error: dict = {}

        async def _producer():
            try:
                async for ev in agen:
                    await queue.put(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 原样交回消费侧抛出，保留原错误语义
                producer_error["exc"] = e
            finally:
                try:
                    queue.put_nowait(_sentinel)
                except asyncio.QueueFull:
                    pass

        prod = asyncio.create_task(_producer())
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        f"[react] session={session_id} 单轮达到硬时限 "
                        f"{settings.AGENT_TURN_TIMEOUT_SECONDS}s，取消回复任务"
                    )
                    if session_id is not None:
                        activity.set_active(session_id, "react:timeout_cancel")
                    yield {"__turn_timeout__": True}
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    continue  # 心跳：短周期重查 deadline，让取消信号更快穿透
                if item is _sentinel:
                    if "exc" in producer_error:
                        raise producer_error["exc"]
                    return
                yield item
        finally:
            if not prod.done():
                prod.cancel()
            try:
                await asyncio.wait_for(prod, timeout=3.0)
            except BaseException:
                pass
            try:
                await agen.aclose()
            except Exception:
                pass

    async def _resolve_external_execution(self, channel, event, agent=None):
        """处理 RequireExternalExecutionEvent：等待外部执行结果，返回恢复回复的输入事件。

        AskUserChoice 经 InteractionChannel 征求用户选择；CollectRequirements 走同一条
        链路但渲染成批量表单；其他外部工具返回不支持提示。
        用户在暂停态停止（INTERRUPT 哨兵）时返回 UserInterruptEvent，
        reply_stream 会以 INTERRUPTED 干净收尾被暂停的回复。

        ``agent`` 仅用于质量指标记账，允许为 None。
        """
        results = []
        for tc in getattr(event, "tool_calls", []):
            try:
                args_obj = json.loads(tc.input) if tc.input else {}
            except (json.JSONDecodeError, TypeError):
                args_obj = {}
            if tc.name == "AskUserChoice":
                question = str(args_obj.get("question") or "")
                norm_options = []
                for i, opt in enumerate((args_obj.get("options") or [])[:6]):
                    label = str((opt or {}).get("label") or f"方案{chr(65 + i)}")
                    desc = str((opt or {}).get("description") or "")
                    norm_options.append({"key": chr(65 + i), "label": label, "description": desc})
                if channel is None:
                    # REST 等非交互链路：兜底文案让模型自行决策
                    answer = NO_CHANNEL_TEXT
                else:
                    answer = await channel.ask_external(
                        request_id=str(tc.id),
                        reply_id=getattr(event, "reply_id", ""),
                        question=question,
                        options=norm_options,
                    )
            elif tc.name == "CollectRequirements":
                answer = await self._resolve_requirements(channel, tc, event, args_obj, agent)
            else:
                answer = f"外部执行工具 {tc.name} 暂不被本平台支持，请改用其他方式完成任务或向用户说明。"
            if answer is INTERRUPT:
                # 用户在暂停态停止：干净收尾被暂停的回复
                return UserInterruptEvent(reply_id=getattr(event, "reply_id", ""))
            results.append(ToolResultBlock(
                type="tool_result",
                id=tc.id,
                name=tc.name,
                output=str(answer),
                state=ToolResultState.SUCCESS,
            ))
        return ExternalExecutionResultEvent(
            reply_id=getattr(event, "reply_id", ""),
            execution_results=results,
        )

    @staticmethod
    async def _resolve_requirements(channel, tc, event, args_obj: dict, agent=None) -> str:
        """CollectRequirements 的外部执行：推表单、等提交、整理成紧凑结果文本。

        单独抽成方法，避免 _resolve_external_execution 随交互类型增多而膨胀；
        题目规整与结果格式化都在 tools/collect_requirements.py 里（与 schema 同源）。
        """
        from app.agents.tools import collect_requirements as cr

        questions = cr.normalize_questions(args_obj.get("questions"))
        mode = str(args_obj.get("mode") or "intake").strip().lower()
        if mode not in ("intake", "interview"):
            mode = "intake"
        if not questions:
            return (
                "CollectRequirements 调用无效：questions 为空。"
                "请补齐 3~8 个具体问题（每题带 assume 默认值）后重试，"
                "或直接按你自己的最优假设继续并在交付说明里列出假设。"
            )
        # 采集记账只能在这里做：外部执行工具不走 acting 中间件管道
        # （框架直接发 RequireExternalExecutionEvent 暂停回复），中间件拦不到。
        if agent is not None:
            try:
                from app.agents import quality_metrics as _qm0
                _metrics = {"intake_calls": 1, "intake_questions": len(questions)}
                if mode == "interview":
                    _metrics["interview_calls"] = 1
                _qm0.record(agent, **_metrics)
            except Exception:
                pass
        if channel is None:
            # REST 等非交互链路：无法采集，直接让模型按 assume 继续
            return FORM_NO_CHANNEL_TEXT
        raw = await channel.ask_external_form(
            request_id=str(tc.id),
            reply_id=getattr(event, "reply_id", ""),
            context=str(args_obj.get("context") or ""),
            questions=questions,
            timeout=settings.COLLECT_REQUIREMENTS_TIMEOUT_SECONDS,
        )
        if raw is INTERRUPT:
            return INTERRUPT

        # 记账：多少题按 assume 兜底了。这个数只有用户提交后才知道，
        # 所以不能像 intake_calls 那样在中间件里按调用参数记。
        if agent is not None:
            try:
                from app.agents import quality_metrics as _qm

                answered = {}
                if isinstance(raw, str) and raw.strip().startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            answered = parsed
                    except (ValueError, TypeError):
                        answered = {}
                assumed_n = 0
                for q in questions:
                    val = answered.get(q.get("id"))
                    if isinstance(val, list):
                        val = [x for x in val if str(x).strip()]
                    if not (str(val).strip() if val is not None else ""):
                        assumed_n += 1
                _qm.record(agent, intake_assumed=assumed_n)
            except Exception:
                pass

        # 超时/跳过时 channel 返回的是整段兜底文案（非 JSON），原样透传给模型
        if isinstance(raw, str) and raw.strip().startswith("{"):
            return cr.format_result(questions, raw, mode=mode)
        if isinstance(raw, str) and not raw.strip():
            return cr.format_result(questions, {}, mode=mode)
        return str(raw)

    @staticmethod
    async def _close_mcp_clients(agent) -> None:
        """尽力关闭 Agent 绑定的 MCP 连接（缓存淘汰/清理时调用，失败忽略）"""
        for client in getattr(agent, "_mcp_clients", None) or []:
            try:
                await client.close(ignore_errors=True)
            except (Exception, asyncio.CancelledError):
                # stdio MCP 的 anyio 收尾可能抛 CancelledError（BaseException，
                # 不被 except Exception 捕获）；清理路径一律忽略，不阻塞缓存淘汰
                pass

    def clear_cache(self):
        running = OrderedDict(
            (key, agent) for key, agent in self.agents_cache.items()
            if self._session_running(key)
        )
        agents = [agent for key, agent in self.agents_cache.items() if key not in running]
        self.agents_cache = running
        self._last_used = {key: self._last_used[key] for key in running}
        self._key_sessions = {key: self._key_sessions[key] for key in running}
        self._invalidated.update(running)
        # 有事件循环时异步释放资源（MCP 连接/workspace）；同步上下文（如管理端配置变更触发）
        # 无循环可挂，连接由 GC/进程回收兜底
        try:
            loop = asyncio.get_running_loop()
            for agent in agents:
                task = loop.create_task(self._release_agent_resources(agent))
                self._release_tasks.add(task)
                task.add_done_callback(self._release_tasks.discard)
        except RuntimeError:
            pass
        logger.info("[AgentScope] Agent缓存已清理，%d 个运行中实例待结束后失效", len(running))


agent_manager = AgentManager()
