"""ReME long-term memory integration layer.

Responsibilities:
- Per-user shared ReMeMiddleware registry: all Agents/sessions of one user share
  a single embedded ReMe app; per-conversation isolation comes from the
  agent's pinned ``state.session_id`` (read live by the middleware).
- On first use, auto-create the user's "personal memory KB" (vault-type,
  scope=personal, invisible to admins) and point ReME's workspace_dir at its
  vault directory, so memory cards written by ReME flow into the existing KB
  pipeline (watchdog auto-index -> vectors + wikilink graph -> SearchKnowledge).
- Lifecycle janitor (``run_janitor``, driven by the scheduler in main.py):
    daily  : 1) session sweep — pull every session transcript under
                session/dialog/, extract unprocessed ones into daily/<date>/*.md
                via LLM (chit-chat judged worthless is dropped), then delete the
                absorbed transcripts; transcripts of still-active sessions
                (modified within the last 10 minutes) are left for the next run
             2) next-day absorption: distill every day directory strictly
                before today into digest/day/<date>.md via LLM, then delete the
                absorbed daily cards + day index + session transcripts;
                persistent facts of the day are merged into the profile card
                (用户画像.md), and an auto link profile -> the day digest
                is recorded in the DB (graph chain no longer needs the index)
    weekly : aggregate daily digests of each finished ISO week into
             digest/week/weekly-<YYYY>-W<WW>.md, then delete the absorbed day
             digests (their content lives on inside the week card)
    monthly: aggregate each finished month's weekly cards (fallback: daily
             digests) into digest/month/monthly-<YYYY-MM>.md, then delete the
             absorbed week cards. Catch-up style — any finished month without a
             month card is aggregated, so a missed 03:00 run on the 1st no
             longer loses the month for good.
    always : purge mem_session/agentscope/*.jsonl older than 2 days (write-only
             auto_memory agent logs, never replayed) and run ReME's native
             optimize_index to physically reclaim tombstoned index entries
  Layered absorption: every level deletes its sources only after its own card
  was written successfully, so nothing is ever lost mid-flight. The memory graph
  keeps its chain through the DB auto links (profile -> day/week/month card) plus
  the ``absorbed`` frontmatter, which stays as textual provenance after the
  source files are gone.

  Digest paths (digest/day|week|month) are owned by ``app.agents.memory_layout``;
  nothing here hardcodes them, and that module still reads the pre-migration flat
  ``digest/`` layout so an un-migrated vault keeps working.
"""

import logging

logger = logging.getLogger(__name__)

import os
import re
import threading
import json
import hashlib
import asyncio
import time
from datetime import datetime, timedelta, date as _date_type
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

from agentscope.message import SystemMsg, UserMsg
from agentscope.middleware import ReMeMiddleware

from app.config import settings
from app.agents import memory_layout as mlayout
from app.agents.memory_folding import batch_material, fold_structured

_REME_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "reme_config.yaml")

# Personal memory KBs are named f"{MEMORY_KB_PREFIX}{user_id}" (KnowledgeBase.name
# is globally unique); janitor discovery matches on this prefix.
MEMORY_KB_PREFIX = "\u6211\u7684\u8bb0\u5fc6-"  # "我的记忆-"

# 记忆库中心的用户画像卡（明确要求记住/偏好与习惯/背景），图谱 hub 节点
PROFILE_NOTE_NAME = "\u7528\u6237\u753b\u50cf.md"  # "用户画像.md"

_middlewares: dict[int, ReMeMiddleware] = {}
_prov_lock = threading.Lock()

# ---------------------------------------------------------------------------
# ReME 防卡死护卫：超时 / 并发 / 计时（延迟优化 P0）
# ---------------------------------------------------------------------------
# memory_search 超时兜底（秒）：官方 ReMeMiddleware._ensure_started/_search 无超时
# 也无日志，嵌入式应用首次启动若卡住（向量索引/嵌入模型懒加载），
# memory_search 会无声阻塞 ReAct 主循环数分钟（服务器实测 5 分钟）。
# 超时抛错由工具层返回"暂不可用"，让模型继续任务。
MEMORY_SEARCH_TIMEOUT = float(os.getenv("MEMORY_SEARCH_TIMEOUT", "25"))
# 搜索超时后在该窗口（秒）内降级快速失败，避免后续每次调用都重复等满超时
MEMORY_SEARCH_DEGRADE_SECONDS = float(os.getenv("MEMORY_SEARCH_DEGRADE_SECONDS", "600"))
# A2：auto_memory 写回（每轮回复后的对话提炼）改后台执行；单次提炼任务的
# 硬超时（秒）——提炼是 LLM 调用，正常 10s 量级，异常时不允许无限跑
MEMORY_WRITEBACK_TIMEOUT_SECONDS = float(
    os.getenv("MEMORY_WRITEBACK_TIMEOUT_SECONDS", str(settings.MEMORY_WRITEBACK_TIMEOUT_SECONDS))
)

# ReME _run_job / app.run_job 硬超时（秒）：所有 ReME 操作（search / auto_memory /
# write / optimize_index / index_paths）的底层入口。官方实现内部做 embedding API
# 调用、向量索引检索、BM25 检索等同步 IO，直接在 asyncio 事件循环上跑会阻塞
# 整个后端（实测 300 秒）。护卫版用 asyncio.to_thread 把同步段丢进线程池，
# 外面套 wait_for 硬超时——超时后事件循环立即恢复，线程池里的调用自然跑完无副作用。
MEMORY_RUN_JOB_TIMEOUT = float(
    os.getenv("MEMORY_RUN_JOB_TIMEOUT", str(settings.MEMORY_RUN_JOB_TIMEOUT))
)

# 索引同步（写入侧兜底）：直写磁盘的记忆卡（用户画像.md 等根级卡）落盘后立刻
# 进 ReME 检索索引，不等 watch 轮询、也不依赖容器重启时的全量对账。
# job 名与超时都可环境变量覆盖，便于排障时临时改行为。
MEMORY_INDEX_JOB = os.getenv("MEMORY_INDEX_JOB", "index_paths")
MEMORY_INDEX_TIMEOUT = float(os.getenv("MEMORY_INDEX_TIMEOUT", "30"))

# 后台预热任务引用表（防 GC 回收任务）
_prewarm_tasks: set = set()
# 后台提炼任务引用表（防 GC 回收任务；shutdown 时统一取消）
_writeback_tasks: set = set()

# ---------------------------------------------------------------------------
# auto_memory 计量与节流（P2-1）
# 背景：每条回复后 ReMe 都跑一次内部 LLM 提炼（ReAct agent，max_iters=30），
# 其 token 不进任何会话统计，是平台的隐性成本。这里补两件事：
# 1) 计量：每次写回记录 开始/完成/跳过/超时 计数与输入字符量（日志 + 模块级
#    统计，供后续监控接口聚合）；
# 2) 节流：纯寒暄/超短回复（无工具调用且助手文本 < MEMORY_WRITEBACK_MIN_REPLY_CHARS）
#    没有记忆价值，直接跳过提炼（0 = 不跳过，行为与改造前一致）。
# ---------------------------------------------------------------------------
_writeback_stats: dict = {
    "started": 0,    # 进入提炼（含完成/超时/失败）
    "skipped": 0,    # 被节流规则跳过
    "done": 0,
    "timeout": 0,
    "failed": 0,
    "chars_in": 0,   # 累计输入消息字符数（token 量的下界代理）
}


def writeback_stats() -> dict:
    """auto_memory 写回统计快照（监控/排障用，只读）。"""
    return dict(_writeback_stats)


def _msg_block_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if not text and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _msg_has_tool_call(msg) -> bool:
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return False
    for block in content:
        btype = getattr(block, "type", None)
        if btype is None and isinstance(block, dict):
            btype = block.get("type")
        if btype in ("tool_call", "tool_use", "tool_result"):
            return True
    return False


def _should_skip_writeback(messages) -> tuple:
    """判定本轮回复是否不值得提炼。返回 (skip: bool, reason: str, assistant_chars: int)。

    规则：无工具调用 且 助手文本总量 < MEMORY_WRITEBACK_MIN_REPLY_CHARS → 跳过
    （纯寒暄/一句话答复没有事实可沉淀）。配置 <=0 时一律不跳过。
    """
    min_chars = int(getattr(settings, "MEMORY_WRITEBACK_MIN_REPLY_CHARS", 0) or 0)
    if min_chars <= 0:
        return False, "", 0
    if any(
        getattr(msg, "role", None) == "user"
        and re.search(r"记住|记下来|请记|不要忘记|remember (?:this|that|my)", _msg_block_text(msg), re.I)
        for msg in messages or []
    ):
        return False, "explicit_memory", 0
    has_tool = False
    assistant_chars = 0
    for msg in messages or []:
        if _msg_has_tool_call(msg):
            has_tool = True
        if getattr(msg, "role", None) == "assistant":
            assistant_chars += len(_msg_block_text(msg))
    if has_tool:
        return False, "", assistant_chars
    if assistant_chars < min_chars:
        return True, f"无工具调用且助手文本仅 {assistant_chars} 字符", assistant_chars
    return False, "", assistant_chars


# ---------------------------------------------------------------------------
# 线程池执行辅助：把 ReME 的 async run_job 丢进独立线程 + 独立事件循环，
# 防止其内部同步 IO（embedding API / 向量索引 / BM25）阻塞主事件循环。
# 模式与 headroom_compression.py 的 SmartCrusher 一致：超时后事件循环立即
# 恢复，线程池里的调用自然跑完无副作用。
# ---------------------------------------------------------------------------
async def _run_app_job_in_thread(app, job: str, /, *, timeout: float = MEMORY_RUN_JOB_TIMEOUT, **kwargs):
    """在线程池中运行 app.run_job，避免同步 IO 阻塞主事件循环。

    app.run_job 是 async def，但内部做 embedding/向量检索/BM25 等同步操作。
    用 asyncio.run 在线程内创建独立事件循环执行，主循环通过 to_thread + wait_for
    获得真正的超时取消能力（wait_for 取消的是 await，线程自然跑完）。

    ``job`` 与 ``app`` 都是 positional-only（``/``）：ReME 的作业载荷里本身就有
    叫 ``name`` 的字段（write / daily_write），若作业名参数也叫 name，调用方传
    ``name=`` 会在实参绑定阶段直接 TypeError（"got multiple values for argument
    'name'"），janitor 整条流水线静默停摆。加 ``/`` 后任何字段名都不可能再撞。
    """
    def _sync():
        return asyncio.run(app.run_job(job, **kwargs))
    return await asyncio.wait_for(
        asyncio.to_thread(_sync),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Structured outputs for the janitor's LLM calls
# ---------------------------------------------------------------------------
class _DistilledNote(BaseModel):
    name: str = Field(description="简洁的主题名（kebab-case 英文或简短中文）")
    description: str = Field(description="对记忆内容的详尽总结")
    content: str = Field(description="提炼后的长期记忆正文（Markdown）")
    tags: List[str] = Field(default_factory=list, description="3~5 个主题标签（短语，不带 # 号），用于知识图谱聚类")


class _MonthlyNote(BaseModel):
    description: str = Field(description="对月度记忆卡的详尽总结")
    content: str = Field(description="聚合后的月度记忆正文（Markdown）")
    tags: List[str] = Field(default_factory=list, description="3~5 个主题标签（短语，不带 # 号），用于知识图谱聚类")


class _SessionExtract(BaseModel):
    worth: bool = Field(description="对话是否包含值得长期记忆的实质性信息（纯寒暄/闲聊/空对话为 false）")
    name: str = Field(description="简洁、稳定的主题/事件文件名 stem（kebab-case 英文或简短中文，不含日期与非法文件名字符）；worth=false 时填空字符串")
    description: str = Field(description="对记忆内容的详尽总结；worth=false 时填空字符串")
    content: str = Field(description="提炼后的记忆正文（Markdown），保留值得长期记住的事实；worth=false 时填空字符串")
    tags: List[str] = Field(default_factory=list, description="3~5 个主题标签（短语，不带 # 号），用于知识图谱聚类")


class _ProfileUpdate(BaseModel):
    has_update: bool = Field(description="新素材中是否包含值得进画像的持久信息（纯临时/一次性内容为 false）")
    content: str = Field(description="合并后的完整用户画像正文（Markdown，不含 frontmatter 与一级标题）；has_update=false 时填空字符串")
    tags: List[str] = Field(default_factory=list, description="3~5 个主题标签（短语，不带 # 号）")


class _ProfileCompact(BaseModel):
    changed: bool = Field(description="清洗是否产生了变化（内容已足够精简、无需清洗时为 false）")
    content: str = Field(description="清洗压缩后的完整用户画像正文（Markdown，不含 frontmatter 与一级标题）；changed=false 时填空字符串")
    tags: List[str] = Field(default_factory=list, description="3~5 个主题标签（短语，不带 # 号）")


_PROFILE_SYSTEM = (
    "你是用户画像维护系统。任务是把新的记忆素材合并进用户画像卡。"
    "画像只收三类持久信息："
    "① 用户明确要求记住的事（'请记住/记住/以后都用'这类明确请求）；"
    "② 宽泛持久的偏好与习惯（代码生成风格、报告/文档格式偏好、沟通偏好、技术栈倾向、饮食生活习惯等）；"
    "③ 稳定的身份与专业背景。"
    "严格排除：具体任务与项目记录（哪怕反复出现也不收）、临时进度与状态、一次性事件。"
    "判断'反复'只看偏好与请求：反复提出的偏好/要求进画像，反复做的任务/项目不进。"
    "以现有画像为基础合并去重：保留原有重要条目，"
    "把『待整合记录』里的条目并入相应分节（不要留在待整合区）。"
    "持久属性（偏好、习惯等）发生变化时不要覆盖旧值：记成'当前值在前+带日期的演变轨迹'，"
    "例如'饮食偏好：喜欢草莓（2026-08-28 起）；此前喜欢香蕉（2026-08-26）'，"
    "每条属性最多保留最近 3 次演变，更早的由每周清洗压缩。"
    "输出完整 Markdown 正文，只按三个二级标题分节：明确要求记住、偏好与习惯、背景；空分节省略。"
)

_COMPACT_SYSTEM = (
    "你是用户画像清洗系统。任务是把现有用户画像卡压缩精简。"
    "规则：合并重复与同类条目；偏好演变压缩为'最新状态+一句演变概述'；"
    "清除混入的任务/项目类、临时状态类内容。"
    "红线：用户明确要求记住的条目只允许合并去重，绝不允许删除。"
    "输出完整 Markdown 正文，只按三个二级标题分节：明确要求记住、偏好与习惯、背景；空分节省略。"
)


_DISTILL_SYSTEM = (
    "你是记忆整合系统。任务是把一天的记忆卡片（由对话提炼的摘要）整合为长期记忆。"
    "保留值得长期记住的事实：用户的持久信息（角色、偏好、习惯）、重要决策及其原因、"
    "项目进展与待办、可复用的方案与结论。去掉寒暄、已过时的临时细节、重复内容。"
    "合并同一主题的多条记录；时间线类内容按时间顺序整理。输出结构清晰的 Markdown。"
)

_SESSION_EXTRACT_SYSTEM = (
    "你是自动记忆系统。任务是从一段完整对话记录中提炼值得长期记忆的信息。"
    "像人类一样思考：哪些信息未来会有用、难以重新获取？"
    "包括：关于用户的持久事实（身份、偏好、习惯）、发生了什么与决策及原因、"
    "当前状态（进度、卡点、下一步）、可直接复用的操作步骤或方案。"
    "用户创作的或要求创作的内容（诗歌、文章、代码、故事等）也必须记录，"
    "尽量保留完整原文，不要只记一句'写了一首诗'。"
    "要全面——每条值得保留的事实都应出现，关键处逐字引用原始措辞或数字。"
    "纯寒暄、闲聊或没有实质内容的对话判定 worth=false。"
    "输出结构清晰的 Markdown 正文；主题名 name 用简洁稳定的文件名风格，不含日期。"
    "同一主题的多条信息必须合并进一张记忆卡，不要为同一事实拆分出多张卡。"

)

_MONTHLY_SYSTEM = (
    "你是记忆整合系统。任务是把上月的全部提炼记忆聚合为一张月度记忆卡。"
    "按主题组织（用户画像、决策记录、项目与进展、方案与结论等），"
    "合并重复事实，保留关键细节与数字，去掉已过时的临时状态。"
    "输出结构清晰、可独立阅读的 Markdown。"
)


# ---------------------------------------------------------------------------
# Middleware registry + personal memory KB provisioning
# ---------------------------------------------------------------------------
def get_memory_middleware(user_id: Optional[int]) -> Optional[ReMeMiddleware]:
    """Return the user's shared ReME middleware (provisioned lazily on first use).

    Returns None when user_id is missing or provisioning fails; chat then
    proceeds without long-term memory rather than erroring out.
    """
    if user_id is None:
        return None
    mw = _middlewares.get(user_id)
    if mw is not None:
        return mw
    with _prov_lock:
        mw = _middlewares.get(user_id)
        if mw is not None:
            return mw
        try:
            kb_id, vault_dir = _ensure_memory_kb(user_id)
            # 启动对账：清掉磁盘上已不存在的悬空文档（连同向量），
            # 兜底重命名/删除事件丢失留下的脏数据
            _reconcile_vault_docs(kb_id, vault_dir)
            # KB watcher: auto-index memory cards into vectors + wikilink graph
            from app.services.knowledge.watcher import KnowledgeWatcher
            KnowledgeWatcher().start_watch(kb_id, vault_dir)
            from app.agents.model_factory import get_model
            from app.services.knowledge_service import get_embedding_model
            mw = ReMeMiddleware(
                workspace_dir=vault_dir,
                config=_REME_CONFIG_PATH,
                parameters=ReMeMiddleware.Parameters(
                    chat_model=get_model(settings.MODEL_NAME),
                    embedding_model=get_embedding_model(),
                    mode="agent_control",
                    top_k=5,
                ),
            )
        except Exception as e:
            logger.error(f"[memory] user={user_id} 记忆中间件初始化失败（聊天不受影响）: {e}")
            return None
        _install_reme_guards(mw, user_id)
        _middlewares[user_id] = mw
        logger.info(f"[memory] user={user_id} 记忆中间件就绪 workspace={vault_dir}")
        return mw


def _install_reme_guards(mw: ReMeMiddleware, user_id: int) -> None:
    """给 ReME 中间件加锁串行化 + 计时日志 + 搜索超时降级。

    实例属性赋值不修改官方类；其内部 self._ensure_started() /
    self._search() / self._run_job() 调用都会走护卫版本。
    """
    if getattr(mw, "_tp_guarded", False):
        return
    mw._tp_guarded = True  # type: ignore[attr-defined]

    start_lock = asyncio.Lock()
    orig_ensure = mw._ensure_started

    async def guarded_ensure_started() -> None:
        async with start_lock:
            if mw._started:  # noqa: SLF001
                return
            logger.info(f"[memory] user={user_id} ReME app starting (first use)...")
            t0 = time.monotonic()
            # A4：启动也走线程池——orig_ensure 内部可能做同步文件扫描/索引加载，
            # 虽然实测仅 0.2s，但防御性包一层避免首次启动卡住事件循环。
            def _sync_ensure():
                asyncio.run(orig_ensure())
            await asyncio.to_thread(_sync_ensure)
            logger.info(f"[memory] user={user_id} ReME app ready in {time.monotonic() - t0:.1f}s")

    orig_search = mw._search
    mw._tp_degraded_until = 0.0  # type: ignore[attr-defined]

    async def guarded_search(query: str, *, limit=None):
        if time.monotonic() < getattr(mw, "_tp_degraded_until", 0.0):
            raise RuntimeError("记忆检索暂不可用（此前检索超时，冷却中）")
        t0 = time.monotonic()
        try:
            # A1：orig_search 内部调 _run_job 做 embedding/向量检索/BM25，全是同步 IO。
            # 用 asyncio.to_thread + asyncio.run 把整个搜索丢进独立线程的独立事件循环，
            # 主循环通过 wait_for 获得真正的超时取消能力——超时后主循环立即恢复，
            # 线程池里的搜索自然跑完无副作用（与 headroom_compression 的 crush 同模式）。
            def _sync_search():
                return asyncio.run(orig_search(query, limit=limit))
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_search),
                timeout=MEMORY_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            mw._tp_degraded_until = time.monotonic() + MEMORY_SEARCH_DEGRADE_SECONDS  # type: ignore[attr-defined]
            logger.error(
                f"[memory] user={user_id} memory_search timeout ({MEMORY_SEARCH_TIMEOUT:.0f}s), "
                f"degraded for {MEMORY_SEARCH_DEGRADE_SECONDS:.0f}s, query={str(query)[:80]!r}"
            )
            raise RuntimeError("memory search timed out")
        finally:
            elapsed = time.monotonic() - t0
            if elapsed > 5:
                logger.warning(f"[memory] user={user_id} memory_search slow: {elapsed:.1f}s")

    orig_run_job = mw._run_job

    # name 同样是 positional-only：与 _run_app_job_in_thread 一致，杜绝作业载荷里
    # 的同名字段（write/daily_write 的 name=）在实参绑定阶段撞车。
    async def guarded_run_job(name, /, **kwargs):
        if settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED and name not in ("search", "index_paths"):
            raise RuntimeError("长期记忆写入已切换到人工审核，禁止直接执行 ReME 写作业")
        # A2：_run_job 是 ReME 所有操作（search / auto_memory / write / edit /
        # optimize_index / index_paths）的底层入口，内部做 embedding API 调用、
        # 向量索引检索、BM25 检索等同步 IO，直接在事件循环上跑会阻塞整个后端
        # （实测 300 秒）。用 asyncio.to_thread + asyncio.run 丢进独立线程，
        # 外面套 wait_for 硬超时——超时后事件循环立即恢复，线程自然跑完无副作用。
        # search 操作额外检查 degraded 标志，超时后冷却期内快速失败。
        if name == "search" and time.monotonic() < getattr(mw, "_tp_degraded_until", 0.0):
            raise RuntimeError("记忆检索暂不可用（此前检索超时，冷却中）")
        t0 = time.monotonic()
        try:
            def _sync_run_job():
                return asyncio.run(orig_run_job(name, **kwargs))
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_run_job),
                timeout=MEMORY_RUN_JOB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if name == "search":
                mw._tp_degraded_until = time.monotonic() + MEMORY_SEARCH_DEGRADE_SECONDS  # type: ignore[attr-defined]
            logger.error(
                f"[memory] user={user_id} ReME job '{name}' timeout "
                f"(>{MEMORY_RUN_JOB_TIMEOUT:.0f}s), 已丢线程池不阻塞事件循环"
            )
            raise RuntimeError(f"memory {name} timed out")
        finally:
            elapsed = time.monotonic() - t0
            if elapsed > 10:
                logger.warning(f"[memory] user={user_id} ReME job '{name}' slow: {elapsed:.1f}s")

    mw._ensure_started = guarded_ensure_started  # type: ignore[attr-defined]
    mw._search = guarded_search  # type: ignore[attr-defined]
    mw._run_job = guarded_run_job  # type: ignore[attr-defined]

    # A2：记忆提炼改后台任务。官方 ReMeMiddleware.on_reply 在回复结束后
    # `await self._write_back(...)`——auto_memory 是 LLM 提炼（实测 10s+，
    # 异常时分钟级），原本卡在回复关键路径末尾占用事件循环。这里改成
    # 即发即忘的后台任务：回复立即返回，提炼异步进行；按 session_id 链式
    # 串行保证同一会话写入顺序；单任务超时兜底（A3）。
    async def checked_write_back(messages, session_id):
        if not session_id:
            raise ValueError("missing memory session id")
        from app.database import SessionLocal
        from app.services.memory_policy import MemoryPolicy, session_memory_policy
        with SessionLocal() as db:
            policy = session_memory_policy(db, user_id, str(session_id))
            if not policy.allows_extraction:
                return
            if settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED:
                from app.services.memory_governance import auto_task
                text = "\n".join(
                    f"{message.role}: {_msg_block_text(message)}"
                    for message in messages if getattr(message, "role", None) in ("user", "assistant")
                )
                auto_task(db, user_id, str(session_id), text)
                return
            if policy == MemoryPolicy.CANDIDATE_REVIEW:
                return
        await mw._run_job(
            "auto_memory",
            messages=[message.model_dump(mode="json") for message in messages],
            session_id=session_id,
        )
    mw._tp_wb_tasks = {}  # type: ignore[attr-defined]  # session_id -> 后台提炼任务
    mw._tp_wb_lingering = {}  # type: ignore[attr-defined]

    async def guarded_write_back(messages, session_id) -> None:
        key = str(session_id)

        # P2-1 节流：无工具调用的超短回复（寒暄等）没有记忆价值，跳过提炼
        skip, skip_reason, _chars = _should_skip_writeback(messages)
        if skip:
            _writeback_stats["skipped"] += 1
            logger.info(
                f"[memory] user={user_id} auto_memory 跳过提炼：{skip_reason}"
            )
            return

        async def _job(prev):
            # 等同会话上一轮提炼收尾（不阻塞回复，只在后台任务内部等）
            if prev is not None and not prev.done():
                try:
                    await prev
                except (Exception, asyncio.CancelledError):
                    pass
            lingering = mw._tp_wb_lingering.get(key)
            if lingering is not None and not lingering.done():
                _writeback_stats["skipped"] += 1
                logger.warning("[memory] user=%s session=%s 上次超时提炼仍在运行，本轮跳过", user_id, key)
                return
            t0 = time.monotonic()
            _writeback_stats["started"] += 1
            _writeback_stats["chars_in"] += sum(
                len(_msg_block_text(m)) for m in (messages or [])
            )
            worker = None
            try:
                # A3：orig_write_back 内部调 _run_job（已被 A2 包了 to_thread），
                # 但 write_back 自身可能还有同步 IO（消息序列化/文件写入）。
                # 防御性再包一层 to_thread，确保后台提炼绝不阻塞事件循环。
                def _sync_write_back():
                    asyncio.run(checked_write_back(messages, session_id))
                worker = asyncio.create_task(asyncio.to_thread(_sync_write_back))
                await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=MEMORY_WRITEBACK_TIMEOUT_SECONDS,
                )
                _writeback_stats["done"] += 1
            except asyncio.TimeoutError:
                _writeback_stats["timeout"] += 1
                if worker is not None and not worker.done():
                    mw._tp_wb_lingering[key] = worker
                    _writeback_tasks.add(worker)
                    def _clear_lingering(done_task):
                        _writeback_tasks.discard(done_task)
                        if mw._tp_wb_lingering.get(key) is done_task:
                            mw._tp_wb_lingering.pop(key, None)
                        try:
                            done_task.result()
                        except (Exception, asyncio.CancelledError):
                            logger.warning("[memory] user=%s session=%s 延迟提炼最终失败", user_id, key)
                    worker.add_done_callback(_clear_lingering)
                logger.error(
                    f"[memory] user={user_id} auto_memory 后台提炼超时 "
                    f"(>{MEMORY_WRITEBACK_TIMEOUT_SECONDS:.0f}s)，放弃本次提炼"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _writeback_stats["failed"] += 1
                logger.warning(f"[memory] user={user_id} 后台记忆提炼失败（不影响聊天）: {e}")
            finally:
                elapsed = time.monotonic() - t0
                if elapsed > 10:
                    logger.warning(
                        f"[memory] user={user_id} auto_memory slow: {elapsed:.1f}s（后台）"
                    )

        prev = mw._tp_wb_tasks.get(key)  # type: ignore[attr-defined]
        try:
            task = asyncio.get_running_loop().create_task(_job(prev))
        except RuntimeError:
            # 无运行中事件循环（关停竞态）：放弃提炼，不阻塞
            return
        mw._tp_wb_tasks[key] = task  # type: ignore[attr-defined]
        _writeback_tasks.add(task)
        def _forget(done_task):
            _writeback_tasks.discard(done_task)
            if mw._tp_wb_tasks.get(key) is done_task:
                mw._tp_wb_tasks.pop(key, None)
        task.add_done_callback(_forget)

    mw._write_back = guarded_write_back  # type: ignore[attr-defined]


def prewarm_memory(user_id: Optional[int]) -> None:
    """后台预启动 ReME 嵌入式应用，把首次启动开销移出首条消息关键路径。

    Agent 构建时调用：启动与后续构建步骤（MCP 连接/沙箱初始化）并行，
    模型首次调用 memory_search 时应用通常已热。失败仅记日志，按需懒启动兜底。
    """
    if user_id is None:
        return
    mw = _middlewares.get(user_id)
    if mw is None or getattr(mw, "_started", False):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_prewarm_one(mw, user_id))
    _prewarm_tasks.add(task)
    task.add_done_callback(_prewarm_tasks.discard)


async def _prewarm_one(mw: ReMeMiddleware, user_id: int) -> None:
    try:
        await mw._ensure_started()  # noqa: SLF001 - 走护卫版
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[memory] user={user_id} prewarm 失败（不影响按需启动）: {e}")


# ---------------------------------------------------------------------------
# On-demand index sync（直写卡片 -> 立刻可检索）
# ---------------------------------------------------------------------------
# 背景：ReME 的 index_update_loop 只在启动时全量对账 + 运行期 watch 轮询，而画像卡
# 由本模块直写磁盘（见 _write_profile_file 的 doc id 稳定性说明）。watch 范围已覆盖
# vault 根目录，但轮询有秒级延迟且默认不落盘，因此写入侧再补一次显式同步。
def _index_changes(vault: str, paths: Iterable[str]) -> List[dict]:
    """把路径规范化为 ReME index_paths 的 changes 载荷（绝对路径，越界丢弃）。

    统一给绝对路径是对齐 ReME 内部约定：init_changes_step / watch_changes_step 产出
    的都是绝对路径，而删除分支还要用 to_workspace_relative 把绝对路径折回 workspace
    相对路径去查节点——传相对路径会让删除静默失效（节点查不到即跳过）。
    change 一律填 "added"：coalesce_changes 会按磁盘实际状态改判 added/modified/deleted，
    调用方不必自己判断文件还在不在。
    """
    base = os.path.abspath(vault or "")
    base_key = os.path.normcase(base)
    changes: List[dict] = []
    for raw in paths or []:
        text = str(raw or "").strip()
        if not text or not base:
            continue
        full = text if os.path.isabs(text) else os.path.join(base, text.replace("/", os.sep))
        full = os.path.abspath(full)
        full_key = os.path.normcase(full)
        if full_key != base_key and not full_key.startswith(base_key + os.sep):
            logger.warning(f"[memory] 索引同步跳过越界路径: {raw}")
            continue
        changes.append({"change": "added", "path": full})
    return changes


async def sync_memory_index(app, vault: str, paths: Iterable[str], *, who: str = "") -> bool:
    """把指定卡片即时同步进 ReME 检索索引（chunks + BM25 + 向量）并落盘。

    只处理显式给出的路径，没有全量对账的删除语义，因此不会误伤索引里的其它文档。
    失败/超时只记日志并返回 False：卡片本身已经写成功，索引兜底还有 watch 轮询与
    下次启动的 init_changes，绝不让同步失败阻塞 janitor 或工具调用。
    """
    if app is None:
        return False
    changes = _index_changes(vault, paths)
    if not changes:
        return False
    # 日志用 vault 相对路径，便于和记忆库文档树对上
    listed = [os.path.relpath(c["path"], vault).replace(os.sep, "/") for c in changes]
    t0 = time.monotonic()
    try:
        # A5：索引同步也走线程池——app.run_job 内部做 embedding/向量索引写入，
        # 同步 IO 会阻塞事件循环。用 _run_app_job_in_thread 统一处理。
        resp = await _run_app_job_in_thread(
            app, MEMORY_INDEX_JOB, timeout=MEMORY_INDEX_TIMEOUT, changes=changes
        )
    except asyncio.TimeoutError:
        logger.error(f"[memory] 索引同步超时（>{MEMORY_INDEX_TIMEOUT:.0f}s）{who}: {listed}")
        return False
    except Exception as e:
        logger.warning(f"[memory] 索引同步失败（写入已完成，不影响聊天）{who}: {e}")
        return False
    if getattr(resp, "success", True) is False:
        logger.warning(f"[memory] 索引同步返回失败 {who}: {getattr(resp, 'answer', '')}")
        return False
    logger.info(f"[memory] 索引同步完成 {who}: {listed}（{time.monotonic() - t0:.1f}s）")
    return True


async def sync_memory_index_for_user(user_id: Optional[int], paths: Iterable[str], *, who: str = "") -> bool:
    """按用户取已就绪的 ReME 应用做索引同步（供工具层/服务层调用）。

    只用已缓存且已启动的中间件：没有就安静返回 False，由 watch 轮询与下次启动的
    全量对账兜底——避免在工具调用里触发懒供应（建库、起 watcher）这类副作用。
    """
    mw = _middlewares.get(user_id) if user_id is not None else None
    if mw is None or not getattr(mw, "_started", False):
        return False
    app = getattr(mw, "_app", None)
    vault = os.path.abspath(getattr(mw, "_workspace_dir", "") or "")
    if app is None or not vault:
        return False
    return await sync_memory_index(app, vault, paths, who=who)


def _ensure_memory_kb(user_id: int) -> tuple:
    """Find or create the user's personal memory KB; returns (kb_id, vault_dir).

    库名 = "我的记忆-<username>"。按 owner+前缀定位，改名/用户名变更都不会
    重复建库；存量旧命名（我的记忆-<uid>）在首次供应时顺手迁移。
    """
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase
    from app.models.user import User
    from app.services.knowledge_service import KnowledgeService
    db = SessionLocal()
    try:
        uname = ""
        u = db.query(User).filter(User.id == user_id).first()
        if u is not None:
            uname = str(getattr(u, "username", "") or "").strip()
        want_name = f"{MEMORY_KB_PREFIX}{uname or user_id}"
        kb = db.query(KnowledgeBase).filter(
            KnowledgeBase.created_by == user_id,
            KnowledgeBase.scope == "personal",
            KnowledgeBase.kind == "memory",
        ).first()
        if kb is None:
            kb = db.query(KnowledgeBase).filter(
                KnowledgeBase.created_by == user_id,
                KnowledgeBase.scope == "personal",
                KnowledgeBase.name.like(MEMORY_KB_PREFIX + "%"),
            ).first()
        if kb is None:
            kb = KnowledgeService(db).create_kb(
                user_id=user_id,
                name=want_name,
                description="系统自动创建的个人记忆库：对话记忆自动提炼写入，仅本人可见可检索。",
                scope="personal",
            )
            kb.kind = "memory"
            db.commit()
        elif kb.name.startswith(MEMORY_KB_PREFIX) and kb.name != want_name:
            clash = db.query(KnowledgeBase).filter(KnowledgeBase.name == want_name).first()
            if clash is None:
                kb.name = want_name
                db.commit()
        if kb.kind != "memory":
            kb.kind = "memory"
            db.commit()
        return kb.id, kb.vault_dir
    finally:
        db.close()


def _reconcile_vault_docs(kb_id: int, vault_dir: str) -> None:
    """个人记忆库对账：删除 vault_path 在磁盘上已不存在的悬空文档（连同向量）。

    只在个人记忆库供应路径调用（共享库保持原行为）；只处理 vault_path 非空
    的文档，上传类文档不受影响。兜底事件丢失（如重命名）留下的悬空文档，
    避免前端点开报"读取失败"、检索命中已删内容。
    """
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase, KnowledgeDocument
    from app.services.knowledge.markdown_indexer import MarkdownIndexer
    db = SessionLocal()
    try:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if not kb or not kb.vault_dir or not vault_dir:
            return
        idx = MarkdownIndexer(db)
        docs = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb_id,
            KnowledgeDocument.vault_path.isnot(None),
        ).all()
        removed = 0
        for d in docs:
            if not os.path.exists(os.path.join(vault_dir, d.vault_path)):
                idx.remove_file(kb, os.path.join(vault_dir, d.vault_path))
                removed += 1
        if removed:
            logger.info(f"[memory] 记忆库对账：清理 {removed} 条悬空文档 kb={kb_id}")
    except Exception as e:
        logger.error(f"[memory] 记忆库对账失败 kb={kb_id}: {e}")
    finally:
        db.close()


def _memory_kb_users() -> list:
    """All user ids that own a personal memory KB (for janitor discovery)."""
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeBase.created_by).filter(
            KnowledgeBase.scope == "personal",
            KnowledgeBase.kind == "memory",
        ).all()
        return [r[0] for r in rows if r[0] is not None]
    finally:
        db.close()


async def close_all() -> None:
    """Teardown hook (app shutdown): close every embedded ReMe app."""
    # 先取消在途后台提炼任务，避免写回撞上已关闭的 ReME 应用
    for t in list(_writeback_tasks):
        t.cancel()
    if _writeback_tasks:
        await asyncio.gather(*list(_writeback_tasks), return_exceptions=True)
    for mw in list(_middlewares.values()):
        try:
            await mw.close()
        except Exception:
            pass
    _middlewares.clear()


# ---------------------------------------------------------------------------
# Lifecycle janitor
# ---------------------------------------------------------------------------
async def run_janitor(now: Optional[datetime] = None) -> dict:
    """Run lifecycle maintenance for every personal memory KB.

    Safe to call at any time; only acts on content past the retention window
    (daily) or on finished ISO weeks / months (weekly & monthly aggregation are
    catch-up style, so a missed run is made up by the next one). Failures are
    logged per user and per step and never propagate.
    """
    now = now or datetime.now()
    if settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED:
        from app.services.memory_governance import queue_janitor_candidates
        return await queue_janitor_candidates(now)
    stats = _empty_user_stats()
    stats["users"] = 0
    stats["failed_users"] = 0
    for user_id in _memory_kb_users():
        mw = get_memory_middleware(user_id)
        if mw is None:
            continue
        stats["users"] += 1
        try:
            await mw._ensure_started()  # noqa: SLF001 - embedded app access by design
            app = mw._app  # noqa: SLF001
            vault = os.path.abspath(mw._workspace_dir)  # noqa: SLF001
            s = await _janitor_user(app, vault, now)
            from app.agents.memory_archive import purge_expired
            s["purged_archives"] = await asyncio.to_thread(purge_expired, vault)
            for k, v in s.items():
                stats[k] = stats.get(k, 0) + v
            # 对账：物理文件清理后清掉 app 层文档表/向量的悬空记录。
            # 兜底 watchdog 缺失或 watcher 事件丢失（容器未装 watchdog 时监听为 no-op）
            try:
                kb_id, _vault = _ensure_memory_kb(user_id)
                # A7：对账做 DB 查询 + 文件系统检查 + 向量删除，同步 IO 会阻塞事件循环。
                await asyncio.to_thread(_reconcile_vault_docs, kb_id, vault)
            except Exception as reconcile_err:
                logger.warning(f"[memory-janitor] user={user_id} 对账失败（不影响其余清理）: {reconcile_err}")
        except Exception as e:
            stats["failed_users"] += 1
            logger.error(f"[memory-janitor] user={user_id} 处理失败: {e}")
    # 空转告警：还有该提炼的日子没出卡、或有步骤/用户整个失败，就大声记一条。
    # 这类故障以前只在 stats 里表现为一片 0，连续静默了三天才被发现。
    stuck = stats["pending_days"] or stats["failed_steps"] or stats["failed_users"]
    if stuck:
        logger.error(
            f"[memory-janitor] 本次未跑干净：待提炼 {stats['pending_days']} 天、"
            f"失败步骤 {stats['failed_steps']} 个、失败用户 {stats['failed_users']} 个；"
            f"下次运行会自动重试，也可 POST /api/v1/monitoring/memory/janitor 手动补跑"
        )
    return stats


# ---------------------------------------------------------------------------
# 运行守卫与状态：03:00 定时器与运维手动触发共用一条入口，两次运行不重叠
# （重叠会让同一批源卡被重复提炼/删除），并留下可查询的上一次结果。
# ---------------------------------------------------------------------------
_janitor_lock = asyncio.Lock()
_janitor_tasks: set = set()  # 后台运行的强引用，防 GC 提前回收任务
_janitor_state: dict = {"running": False, "runs": 0, "started_at": None,
                        "finished_at": None, "last_stats": None, "last_error": None}


def janitor_status() -> dict:
    """当前/上一次 janitor 运行的快照（只读副本，运维端点用）。"""
    return dict(_janitor_state)


async def run_janitor_once(now: Optional[datetime] = None, *, wait: bool = True) -> dict:
    """串行触发一次 janitor。

    wait=True：等运行结束并返回 stats（定时器、本地验证用）。
    wait=False：丢后台任务立即返回（HTTP 端点用，避免网关超时掐断请求），
    结果照样写进 janitor_status()。
    已有运行在进行时直接返回 skipped，不排队第二次。
    """
    if _janitor_lock.locked():
        return {"skipped": "已有 janitor 在运行，请稍后查 status", **janitor_status()}
    if not wait:
        task = asyncio.create_task(_run_janitor_guarded(now))
        _janitor_tasks.add(task)
        task.add_done_callback(_janitor_tasks.discard)
        return {"started": True, **janitor_status()}
    return await _run_janitor_guarded(now)


async def _run_janitor_guarded(now: Optional[datetime]) -> dict:
    async with _janitor_lock:
        _janitor_state.update(
            running=True, runs=_janitor_state["runs"] + 1, last_error=None,
            started_at=datetime.now().isoformat(timespec="seconds"), finished_at=None,
        )
        try:
            stats = await run_janitor(now)
            _janitor_state["last_stats"] = stats
            return stats
        except Exception as e:
            _janitor_state["last_error"] = str(e)
            logger.error(f"[memory-janitor] 运行失败: {e}")
            raise
        finally:
            _janitor_state.update(
                running=False,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )


def _empty_user_stats() -> dict:
    """一个用户的 janitor 计数初值（run_janitor 汇总时用同一份键集）。"""
    return {
        "extracted_sessions": 0, "dropped_sessions": 0, "cleaned_sessions": 0,
        "distilled_days": 0, "pending_days": 0, "cleaned_daily": 0,
        "weekly": 0, "monthly": 0, "cleaned_digests": 0,
        "profile_updates": 0, "profile_compacts": 0,
        "cleaned_mem_sessions": 0, "optimized_index": 0,
        "purged_archives": 0, "failed_steps": 0,
    }


# 步骤表：(标签, 协程)，签名统一 (app, vault, now) -> dict（局部计数）。
# 新增生命周期步骤只需往这里加一行，_janitor_user 的隔离与汇总逻辑不用动。
# 用函数而非模块级常量：各步骤实现定义在本表之后，模块加载期取名会 NameError。
def _janitor_steps() -> tuple:
    return (
        # 0) 会话流水 -> 当日记忆卡（排在提炼前，新提取的卡片当天就走正常生命周期）
        ("sessions", _sweep_sessions),
        ("daily", _step_absorb_past_days),
        ("weekly", _step_weekly),
        ("monthly", _step_monthly),
        # 吸收对账：清掉"已被上层总结卡吸收、却还留在磁盘上"的历史源卡
        ("reconcile_absorbed", _step_reconcile_absorbed),
        ("purge_mem_sessions", _step_purge_mem_sessions),
        ("optimize_index", _step_optimize_index),
        ("purge_archives", _step_purge_archives),
    )


async def _janitor_user(app, vault: str, now: datetime) -> dict:
    """跑完一个用户的记忆生命周期；每步独立 try，一步失败不牵连其余步骤。

    此前整条流水线共用一个 try：日提炼里任何异常都会让周/月聚合、过期日志清理、
    索引维护全部跳过（2026-09-05 起连续三天 stats 全 0 就是这么来的）。
    """
    stats = _empty_user_stats()
    for label, step in _janitor_steps():
        try:
            for k, v in (await step(app, vault, now)).items():
                stats[k] = stats.get(k, 0) + v
        except Exception as e:
            stats["failed_steps"] += 1
            logger.error(
                f"[memory-janitor] 步骤 {label} 失败（其余步骤继续）vault={vault}: {e}",
                exc_info=True,
            )
    return stats


async def _step_absorb_past_days(app, vault: str, now: datetime) -> dict:
    """次日吸收：把严格早于今天的日目录提炼为 digest/day/<date>.md，再清理原文。"""
    stats = {"distilled_days": 0, "pending_days": 0, "cleaned_daily": 0,
             "cleaned_sessions": 0, "profile_updates": 0}
    today = now.date()
    daily_root = os.path.join(vault, "daily")
    if not os.path.isdir(daily_root):
        return stats
    for day_dir in sorted(os.listdir(daily_root)):
        day = _parse_date(day_dir)
        if day is None or day >= today:
            continue
        day_path = os.path.join(daily_root, day_dir)
        if not os.path.isdir(day_path):
            continue
        notes = [f for f in os.listdir(day_path) if f.endswith(".md")]
        if not notes:
            continue
        session_ids = _collect_session_ids(day_path, notes)
        digested_hashes = await _distill_day(app, day_dir, day_path, notes)
        if digested_hashes is None:
            # 提炼失败：原文一张都不动，计入 pending，下次运行重试
            stats["pending_days"] += 1
            continue
        stats["distilled_days"] += 1
        # 当日卡片中的持久信息并入用户画像（失败不阻塞后续清理）
        profile_updated = await _update_profile(app, vault, day_dir, day_path, notes)
        if profile_updated is None:
            stats["pending_days"] += 1
            continue
        if profile_updated:
            stats["profile_updates"] += 1
        # 画像->当日总结 auto 边：提炼成功即落边（时间线边，与画像是否变化无关）
        _link_profile_to_card(
            vault, mlayout.rel_path(mlayout.DAY, day_dir), f"当日总结-{day_dir}"
        )
        # 清理已被吸收的原文：日卡片 + 当日索引（画像->总结 auto 边已落库，
        # 图谱链不再依赖当日索引锚点；索引次日消化后即删）
        from app.agents.memory_archive import archive_source
        digest_rel = mlayout.rel_path(mlayout.DAY, day_dir)
        archived_all = True
        for fn in notes:
            source_rel = f"daily/{day_dir}/{fn}"
            if await archive_source(app, vault, source_rel, digest_rel,
                                    expected_hash=digested_hashes.get(source_rel)):
                stats["cleaned_daily"] += 1
            else:
                archived_all = False
        if not archived_all:
            stats["pending_days"] += 1
            continue
        if any(name.endswith(".md") for name in os.listdir(day_path)):
            stats["pending_days"] += 1
            continue
        if os.path.isfile(os.path.join(daily_root, f"{day_dir}.md")):
            if not await archive_source(app, vault, f"daily/{day_dir}.md", digest_rel):
                stats["pending_days"] += 1
        _rmdir_if_empty(day_path)
        # 不再被任何日卡片引用的会话流水一并清掉
        for sid in session_ids:
            if not _session_referenced_elsewhere(vault, sid):
                if os.path.isfile(os.path.join(vault, "session", "dialog", f"{sid}.jsonl")):
                    if await archive_source(app, vault, f"session/dialog/{sid}.jsonl", digest_rel):
                        stats["cleaned_sessions"] += 1
    return stats


async def _step_weekly(app, vault: str, now: datetime) -> dict:
    """周聚合 + 每周画像清洗。"""
    stats = {"weekly": 0, "cleaned_digests": 0, "profile_compacts": 0}
    weeks_done, absorbed = await _aggregate_week(app, vault, now)
    stats["weekly"] += weeks_done
    stats["cleaned_digests"] += absorbed
    # 出周卡后立即清洗画像；本次没有聚合目标时，也在新一周的首次运行清洗，
    # 保证每个结束周至少清洗一次（红线：明确要求记住的只合并不删）
    week_started = mlayout.iso_week_label(now.date()) != mlayout.iso_week_label(
        (now - timedelta(days=1)).date()
    )
    if (weeks_done or week_started) and await _compact_profile(app, vault):
        stats["profile_compacts"] += 1
    return stats


async def _step_monthly(app, vault: str, now: datetime) -> dict:
    """月聚合（补偿式：任何已结束但还没月卡的月份都会被补上）。"""
    stats = {"monthly": 0, "cleaned_digests": 0}
    months_done, absorbed = await _aggregate_months(app, vault, now)
    stats["monthly"] += months_done
    stats["cleaned_digests"] += absorbed
    return stats


async def _step_reconcile_absorbed(app, vault: str, now: datetime) -> dict:
    """对账删除：已被周/月卡吸收、却还留在磁盘上的源卡。

    出卡后删源卡是现在的约定，但历史版本（d9236ea7 起）刻意保留源卡作图谱锚点，
    存量 vault 里于是留着一批"已吸收却还在"的日/周总结。这一步以总结卡 frontmatter
    的 absorbed 为唯一依据做清理：只删被明确列出的卡，绝不误伤之后新产生的内容，
    因此可以每次运行都跑，天然幂等。
    """
    stats = {"cleaned_digests": 0}
    leftovers: set = set()
    from app.agents.memory_archive import archive_source
    for level in (mlayout.WEEK, mlayout.MONTH):
        for card in mlayout.iter_cards(vault, level):
            for src in _absorbed_existing(vault, card):
                if src.rel in leftovers:
                    continue
                if await archive_source(app, vault, src.rel, card.rel):
                    stats["cleaned_digests"] += 1
                    leftovers.add(src.rel)
    return stats


def _absorbed_existing(vault: str, card: mlayout.Card) -> List[mlayout.Card]:
    """读一张周/月卡的 absorbed 字段，返回其中仍然存在于磁盘上的源卡。"""
    try:
        with open(card.abs, encoding="utf-8") as f:
            meta = _frontmatter(f.read())
    except OSError:
        return []
    out: List[mlayout.Card] = []
    known = _absorbed_hashes(card)
    for entry in str(meta.get("absorbed") or "").split(","):
        for cand in mlayout.absorbed_candidates(entry):
            hit = mlayout.classify(cand)
            if hit is None:
                continue
            found = mlayout.find_card(vault, hit[0], hit[1])
            # 排除自己：周/月卡不会因为写错 absorbed 而自删
            if found is not None and found.rel != card.rel and found.rel in known:
                try:
                    if _hash_sources(vault, [found.rel]).get(found.rel) == known[found.rel]:
                        out.append(found)
                except OSError:
                    pass
            break
    return out


async def _step_purge_mem_sessions(app, vault: str, now: datetime) -> dict:
    """归档过期的 ReME 中间会话日志；原始内容额外保留七天。"""
    stats = {"cleaned_mem_sessions": 0}
    mem_dir = os.path.join(vault, "mem_session", "agentscope")
    if not os.path.isdir(mem_dir):
        return stats
    cutoff = now.timestamp() - 86400
    from app.agents.memory_archive import archive_source
    for fn in os.listdir(mem_dir):
        if not fn.endswith(".jsonl"):
            continue
        fp = os.path.join(mem_dir, fn)
        try:
            if os.path.getmtime(fp) < cutoff:
                if await archive_source(app, vault, f"mem_session/agentscope/{fn}"):
                    stats["cleaned_mem_sessions"] += 1
        except OSError as e:
            logger.warning(f"[memory-janitor] 清理过期会话日志失败 {fp}: {e}")
    return stats


async def _step_optimize_index(app, vault: str, now: datetime) -> dict:
    """空闲期索引维护（ReME 原生 optimize_index_step）：把上面各步删文件留下的
    关键字索引/向量墓碑物理回收。失败绝不影响清理结果。"""
    stats = {"optimized_index": 0}
    try:
        resp = await _run_app_job_in_thread(app, "optimize_index")
        if getattr(resp, "success", True) is not False:
            stats["optimized_index"] += 1
    except Exception as e:
        logger.warning(f"[memory-janitor] optimize_index 失败（不影响其余清理）: {e}")
    return stats


async def _step_purge_archives(app, vault: str, now: datetime) -> dict:
    from app.agents.memory_archive import purge_expired
    return {"purged_archives": await asyncio.to_thread(purge_expired, vault)}


async def _sweep_sessions(app, vault: str, now: datetime) -> dict:
    """Pull every session transcript, extract it into a daily card, then
    delete the transcript (the daily "拉取会话 -> 提取 -> 清理流水" step).

    - transcript already represented by a daily note -> delete transcript only
    - otherwise LLM-extract; write daily card first, delete transcript only
      after the write succeeds (提炼失败绝不删除原文)
    - pure small talk / empty transcript -> nothing to keep, transcript dropped
    - files touched in the last 10 minutes are left for the next run
      (the conversation may still be active)
    """
    stats = {"extracted_sessions": 0, "dropped_sessions": 0, "cleaned_sessions": 0}
    dialog_dir = os.path.join(vault, "session", "dialog")
    if not os.path.isdir(dialog_dir):
        return stats
    active_cutoff = now.timestamp() - 600
    for fn in sorted(os.listdir(dialog_dir)):
        if not fn.endswith(".jsonl"):
            continue
        sid = fn[:-6]
        fpath = os.path.join(dialog_dir, fn)
        try:
            if os.path.getmtime(fpath) > active_cutoff:
                continue
        except OSError:
            continue
        rel = f"session/dialog/{fn}"
        # Already extracted by auto_memory: a daily note references this session
        from app.agents.memory_archive import archive_source
        if _session_referenced_elsewhere(vault, sid):
            if await archive_source(app, vault, rel):
                stats["cleaned_sessions"] += 1
            continue
        outcome = await _extract_session(app, vault, sid, fpath)
        if outcome == "extracted":
            stats["extracted_sessions"] += 1
            if await archive_source(app, vault, rel):
                stats["cleaned_sessions"] += 1
        elif outcome == "dropped":
            stats["dropped_sessions"] += 1
            if await archive_source(app, vault, rel):
                stats["cleaned_sessions"] += 1
        # outcome == "failed": keep the transcript, retry next run
    return stats


async def _extract_session(app, vault: str, sid: str, fpath: str) -> str:
    """LLM-extract one transcript into daily/<day>/<name>.md.

    Returns "extracted" (card written), "dropped" (nothing worth keeping)
    or "failed" (LLM/write error; transcript must be kept).
    """
    turns = _load_transcript(fpath)
    text_len = sum(len(t[2]) for t in turns)
    if not turns or text_len < 30:
        return "dropped"
    day = None
    for _name, created, _text in turns:
        d = _parse_date(str(created)[:10])
        if d is not None:
            day = d  # keep the latest message date
    if day is None:
        try:
            day = datetime.fromtimestamp(os.path.getmtime(fpath)).date()
        except OSError:
            day = datetime.now().date()
    history = "\n\n".join(
        f"[{name or 'user'} @ {created}]\n{text}" for name, created, text in turns
    )
    try:
        res = await fold_structured(
            _SESSION_EXTRACT_SYSTEM,
            f"以下是一段会话记录（session: {sid}），请按要求提炼。",
            [history], _SessionExtract, _llm_structured, worth_field=True,
        )
    except Exception as e:
        logger.error(f"[memory-janitor] session={sid} 提取失败（保留流水下次重试）: {e}")
        return "failed"
    if not res:
        return "failed"
    content = str(res.get("content") or "").strip()
    if not res.get("worth") or not content:
        logger.info(f"[memory-janitor] session={sid} 无实质内容，清理流水")
        return "dropped"
    name = _clean_note_name(str(res.get("name") or ""), f"session-{sid}")
    day_str = day.isoformat()
    if os.path.exists(os.path.join(vault, "daily", day_str, f"{name}.md")):
        suffix = sid.replace("-", "")[-6:] or "x"
        name = f"{name}-{suffix}"
    try:
        # 用 daily_write 而非通用 write：自动补 session_id / source_conversation
        # frontmatter，并扫描当日文件夹重建 daily/<date>.md 的 notes:auto 索引
        # （通用 write 不刷新索引，会导致"当日索引"漏记 janitor 提炼的卡片）
        resp = await _run_app_job_in_thread(
            app,
            "daily_write",
            name=str(res.get("name") or name),
            description=str(res.get("description") or f"session {sid} 提取记忆"),
            session_id=sid,
            content=content,
            date=day_str,
            metadata={"tags": res.get("tags") or []},
        )
    except Exception as e:
        logger.error(f"[memory-janitor] session={sid} 写入记忆卡失败（保留流水）: {e}")
        return "failed"
    if getattr(resp, "success", True) is False:
        logger.error(f"[memory-janitor] session={sid} 写入记忆卡失败（保留流水）: {getattr(resp, 'answer', '')}")
        return "failed"
    logger.info(f"[memory-janitor] session={sid} 提取完成 -> daily/{day_str}/{name}.md")
    return "extracted"


def _load_transcript(fpath: str) -> list:
    """Parse a session jsonl into (speaker, created_at, text) turns (text blocks only)."""
    turns = []
    try:
        with open(fpath, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return turns
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        text = _msg_text(item.get("content"))
        if not text:
            continue
        turns.append((str(item.get("name") or item.get("role") or ""),
                      str(item.get("created_at") or ""), text))
    return turns


def _msg_text(content) -> str:
    """Extract plain text from a message content payload (skip tool blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = str(block.get("text") or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _clean_note_name(name: str, fallback: str) -> str:
    name = _BAD_FILENAME_CHARS.sub("_", str(name or "")).strip(" ._")
    if len(name) > 80:
        name = name[:80].rstrip(" ._")
    return name or fallback


# ---------------------------------------------------------------------------
# 总结卡读写共用小工具：写卡、读源卡、聚合 LLM、删除已吸收源卡。
# 三层聚合（日/周/月）共用同一套错误语义——写不成功就一张源卡都不删。
# ---------------------------------------------------------------------------
async def _write_digest_card(app, rel: str, *, name: str, description: str,
                             content: str, metadata: dict) -> bool:
    """写一张总结卡到 vault 相对路径 rel。True = 写成功。

    异常与业务失败都只记日志、返回 False，绝不冒泡打断 janitor 的其余步骤；
    返回 False 时调用方必须保留全部源卡等下次重试。
    """
    try:
        resp = await _run_app_job_in_thread(
            app, "write", path=rel, name=name,
            description=description, content=content, metadata=metadata,
        )
    except Exception as e:
        logger.error(f"[memory-janitor] 总结卡写入异常 {rel}: {e}")
        return False
    if getattr(resp, "success", True) is False:
        logger.error(f"[memory-janitor] 总结卡写入失败 {rel}: {getattr(resp, 'answer', '')}")
        return False
    return True


def _read_cards(cards: List[mlayout.Card]) -> tuple:
    """读源卡正文，返回 (拼接素材, 真正读到的卡)。

    读不到的卡既不进素材、也绝不会出现在后面的删除清单里。
    """
    parts: List[str] = []
    ok: List[mlayout.Card] = []
    for c in cards:
        try:
            with open(c.abs, encoding="utf-8") as f:
                parts.append(f"## {c.filename}\n{f.read()}")
        except OSError as e:
            logger.warning(f"[memory-janitor] 源卡读取失败（跳过且不删除）{c.rel}: {e}")
            continue
        ok.append(c)
    return parts, ok


def _hash_sources(vault: str, paths: Iterable[str]) -> dict[str, str]:
    hashes = {}
    for relative in paths:
        path = os.path.realpath(os.path.join(vault, relative))
        if not path.startswith(os.path.realpath(vault) + os.sep):
            raise ValueError("memory source outside vault")
        with open(path, "rb") as handle:
            hashes[relative] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def _absorbed_hashes(card: Optional[mlayout.Card]) -> dict[str, str]:
    if card is None:
        return {}
    try:
        with open(card.abs, encoding="utf-8") as handle:
            encoded = _frontmatter(handle.read()).get("absorbed_hashes") or ""
    except OSError:
        return {}
    hashes = {}
    for entry in str(encoded).split(","):
        path, separator, digest = entry.rpartition(":")
        if separator and re.fullmatch("[0-9a-f]{64}", digest):
            hashes[path] = digest
    return hashes


def _format_hashes(hashes: dict[str, str]) -> str:
    return ",".join(f"{path}:{digest}" for path, digest in hashes.items())


async def _aggregate_llm(system: str, lead: str, parts: List[str], model_cls,
                         *, who: str, limit: int = 100000) -> Optional[dict]:
    """周/月共用的有界分批聚合；任何一步失败都保留源卡。"""
    try:
        res = await fold_structured(system, lead, parts, model_cls, _llm_structured)
    except Exception as e:
        logger.error(f"[memory-janitor] {who} 失败（保留源卡下次重试）: {e}")
        return None
    if not res or not str(res.get("content") or "").strip():
        logger.warning(f"[memory-janitor] {who} 未产出正文（保留源卡下次重试）")
        return None
    return res


async def _delete_absorbed(app, vault: str, cards: List[mlayout.Card], *, who: str,
                           digest_rel: str, expected_hashes: dict[str, str]) -> int:
    """删除已被上层总结卡吸收的源卡，并把删除同步进 ReME 检索索引。

    只允许在对应总结卡写成功后调用（调用方保证）。索引这一步不能省：
    reme_config.yaml 里 watch_changes_step 配的是 persist: False，运行期的文件
    变动不落盘，不显式同步的话 memory_search 会长期命中已删卡片。
    """
    if not cards:
        return 0
    from app.agents.memory_archive import archive_source
    deleted = 0
    for card in cards:
        if await archive_source(app, vault, card.rel, digest_rel,
                                expected_hash=expected_hashes.get(card.rel)):
            deleted += 1
    if deleted:
        logger.info(f"[memory-janitor] {who} 已归档 {deleted}/{len(cards)} 张源卡")
    return deleted


async def _distill_day(app, day_dir: str, day_path: str, notes: list) -> Optional[dict]:
    """提炼日卡；返回已吸收源文件的哈希快照，失败返回 None。"""
    vault = os.path.dirname(os.path.dirname(day_path))
    sources = [f"daily/{day_dir}/{fn}" for fn in sorted(notes)]
    try:
        snapshot = _hash_sources(vault, sources)
    except OSError:
        return None
    existing = mlayout.find_card(vault, mlayout.DAY, day_dir)
    hashes = _absorbed_hashes(existing)
    fresh = [fn for fn in sorted(notes) if hashes.get(f"daily/{day_dir}/{fn}") !=
             snapshot[f"daily/{day_dir}/{fn}"]]
    if existing is not None and not fresh:
        return snapshot
    parts = []
    for fn in fresh:
        try:
            with open(os.path.join(day_path, fn), encoding="utf-8") as f:
                parts.append(f"## 记忆卡片: {fn}\n{f.read()}")
        except OSError:
            continue
    if not parts:
        return None
    rel = mlayout.rel_path(mlayout.DAY, day_dir)
    if existing is not None:
        try:
            with open(existing.abs, encoding="utf-8") as handle:
                parts.insert(0, f"## 已有日总结\n{handle.read()}")
        except OSError:
            return None
    try:
        res = await fold_structured(
            _DISTILL_SYSTEM, f"以下是 {day_dir} 的记忆卡片，请提炼为长期记忆。",
            parts, _DistilledNote, _llm_structured,
        )
    except Exception as e:
        logger.error(f"[memory-janitor] {day_dir} 提炼失败（保留原文下次重试）: {e}")
        return None
    if not res or not str(res.get("content") or "").strip():
        return None
    try:
        if _hash_sources(vault, sources) != snapshot:
            return None
    except OSError:
        return None
    hashes.update(snapshot)
    if not await _write_digest_card(
        app, rel,
        name=str(res.get("name") or f"memory-{day_dir}"),
        description=str(res.get("description") or f"{day_dir} 提炼记忆"),
        content=str(res["content"]),
        metadata={"distilled_from": day_dir, "tags": res.get("tags") or [],
                  "absorbed": ",".join(hashes), "absorbed_hashes": _format_hashes(hashes),
                  "title": mlayout.display_title(mlayout.DAY, day_dir)},
    ):
        return None
    logger.info(f"[memory-janitor] {day_dir} 提炼完成 -> {rel}")
    return snapshot


# 月度补偿窗口：最多回看这么多个已结束月份。够长以覆盖长假/停机，又不至于在
# 首次上线时把陈年历史全部重算一遍。
MONTHLY_CATCHUP_MONTHS = int(
    os.getenv("MEMORY_MONTHLY_CATCHUP_MONTHS", str(settings.MEMORY_MONTHLY_CATCHUP_MONTHS))
)


def _shift_month(d, months: int):
    """日期 -> 前/后若干个月的 1 号（date）。"""
    total = d.year * 12 + (d.month - 1) + months
    return _date_type(total // 12, total % 12 + 1, 1)


def _cards_in_month(cards: List[mlayout.Card], month_label: str, *,
                    by_thursday: bool) -> List[mlayout.Card]:
    """挑出归属某个月的总结卡。周卡按该周周四归月（ISO 周多数天所在的月），
    日卡直接按 key 前缀。"""
    out = []
    for c in cards:
        if by_thursday:
            d = mlayout.week_thursday(c.key)
            if d is None or d.strftime("%Y-%m") != month_label:
                continue
        elif not c.key.startswith(month_label):
            continue
        out.append(c)
    return out


def _pending_months(vault: str, now: datetime,
                    limit: int = MONTHLY_CATCHUP_MONTHS) -> List[str]:
    """已结束、还没有月卡的月份（旧 -> 新），最多 limit 个。"""
    this_month = _shift_month(now.date(), 0)
    labels = []
    for i in range(1, max(1, limit) + 1):
        label = _shift_month(this_month, -i).strftime("%Y-%m")
        if mlayout.exists(vault, mlayout.MONTH, label):
            continue  # 已聚合
        labels.append(label)
    labels.reverse()  # 先补最早的，停机多月后不会只补最近一个月
    return labels


async def _aggregate_months(app, vault: str, now: datetime) -> tuple:
    """补偿式月聚合：把所有"已结束且有源卡但还没月卡"的月份逐个聚合。

    旧实现只在 now.day == 1 时跑一次——容器在 1 号 03:00 恰好重启、或那一次抛了
    异常，那个月就永久漏掉。改成与周聚合同构的补偿式，漏跑由后续任意一次运行补上。
    返回 (新建月卡数, 删除的源卡数)。
    """
    done = 0
    deleted = 0
    labels = set(_pending_months(vault, now))
    for card in mlayout.iter_cards(vault, mlayout.WEEK):
        thursday = mlayout.week_thursday(card.key)
        if thursday is not None and thursday < _shift_month(now.date(), 0):
            labels.add(thursday.strftime("%Y-%m"))
    for card in mlayout.iter_cards(vault, mlayout.DAY):
        day = mlayout.parse_day(card.key)
        if day is not None and day < _shift_month(now.date(), 0):
            labels.add(day.strftime("%Y-%m"))
    for label in sorted(labels):
        made, removed = await _aggregate_month(app, vault, label)
        done += made
        deleted += removed
    return done, deleted


async def _aggregate_month(app, vault: str, month_label: str) -> tuple:
    """把一个月的记忆聚合为 digest/month/monthly-<YYYY-MM>.md。

    素材优先该月的周卡；该月一张周卡都没有时回退日总结（例如周聚合长期失败期间
    攒下的存量）。月卡写成功后删除被吸收的源卡。返回 (是否新建, 删除源卡数)。
    """
    existing = mlayout.find_card(vault, mlayout.MONTH, month_label)
    weekly = _cards_in_month(mlayout.iter_cards(vault, mlayout.WEEK), month_label,
                             by_thursday=True)
    daily = _cards_in_month(mlayout.iter_cards(vault, mlayout.DAY), month_label,
                            by_thursday=False)
    sources = weekly or daily
    if not sources:
        return 0, 0
    previous = _absorbed_hashes(existing)
    current = _hash_sources(vault, (card.rel for card in sources))
    fresh = [card for card in sources if previous.get(card.rel) != current[card.rel]]
    if not fresh:
        if existing:
            removed = await _delete_absorbed(app, vault, sources, who="月度补偿",
                                              digest_rel=existing.rel, expected_hashes=current)
            return 0, removed
        return 0, 0
    parts, absorbed = _read_cards(fresh)
    if not parts:
        return 0, 0
    if existing is not None:
        with open(existing.abs, encoding="utf-8") as handle:
            parts.insert(0, f"## 已有月总结\n{handle.read()}")
    who = f"月度聚合 {month_label}"
    res = await _aggregate_llm(
        _MONTHLY_SYSTEM,
        f"以下是 {month_label} 的提炼记忆，请聚合为一张月度记忆卡。",
        parts, _MonthlyNote, who=who,
    )
    if res is None:
        return 0, 0
    if _hash_sources(vault, (card.rel for card in absorbed)) != {
        card.rel: current[card.rel] for card in absorbed
    }:
        return 0, 0
    rel = existing.rel if existing else mlayout.rel_path(mlayout.MONTH, month_label)
    previous.update({card.rel: current[card.rel] for card in absorbed})
    if not await _write_digest_card(
        app, rel,
        name=f"monthly-{month_label}",
        description=str(res.get("description") or f"{month_label} 月度记忆"),
        content=str(res["content"]),
        metadata={"monthly": month_label,
                  "absorbed": ",".join(previous),
                  "absorbed_hashes": _format_hashes(previous),
                  "tags": res.get("tags") or [],
                  "title": mlayout.display_title(mlayout.MONTH, month_label)},
    ):
        return 0, 0  # 写失败：源卡一张不删，下次重试
    _link_profile_to_card(vault, rel, f"月度-{month_label}")
    deleted = await _delete_absorbed(app, vault, absorbed, who=who, digest_rel=rel,
                                     expected_hashes=current)
    logger.info(f"[memory-janitor] {month_label} 月度聚合完成（吸收 {len(absorbed)} 张源卡）")
    return 1, deleted



_WEEKLY_SYSTEM = (
    "你是记忆整合系统。任务是把一周的全部日提炼记忆聚合为一张周记忆卡。"
    "按主题组织（用户画像、决策记录、项目与进展、方案与结论等），"
    "合并重复事实，保留关键细节与数字，去掉已过时的临时状态。"
    "输出结构清晰、可独立阅读的 Markdown。"
)


async def _aggregate_week(app, vault: str, now: datetime) -> tuple:
    """把已结束 ISO 周的日总结聚合为 digest/week/weekly-<YYYY>-W<WW>.md。

    触发条件 = 该周已结束（等价于攒够 7 天日总结）；当前周的日总结不动。
    已有周卡的周不重复聚合。周卡写成功后删除被吸收的日总结——内容已经进了周卡，
    溯源信息留在周卡 frontmatter 的 absorbed 字段与 画像->周卡 的 DB auto 边上。
    返回 (新建周卡数, 删除的源卡数)。
    """
    by_week: Dict[str, List[mlayout.Card]] = {}
    for card in mlayout.iter_cards(vault, mlayout.DAY):
        d = mlayout.parse_day(card.key)
        if d is None:
            continue
        by_week.setdefault(mlayout.iso_week_label(d), []).append(card)
    if not by_week:
        return 0, 0
    current_week = mlayout.iso_week_label(now.date())
    done = 0
    deleted = 0
    for week_label in sorted(by_week):
        if week_label >= current_week:
            continue  # 本周未结束，等攒满
        existing = mlayout.find_card(vault, mlayout.WEEK, week_label)
        cards = sorted(by_week[week_label], key=lambda c: c.key)
        previous = _absorbed_hashes(existing)
        current = _hash_sources(vault, (card.rel for card in cards))
        fresh = [card for card in cards if previous.get(card.rel) != current[card.rel]]
        if not fresh:
            if existing:
                deleted += await _delete_absorbed(app, vault, cards, who="周度补偿",
                                                   digest_rel=existing.rel, expected_hashes=current)
            continue
        parts, absorbed = _read_cards(fresh)
        if not parts:
            continue
        if existing is not None:
            with open(existing.abs, encoding="utf-8") as handle:
                parts.insert(0, f"## 已有周总结\n{handle.read()}")
        who = f"周聚合 {week_label}"
        res = await _aggregate_llm(
            _WEEKLY_SYSTEM,
            f"以下是 {week_label} 一周的日提炼记忆，请聚合为一张周记忆卡。",
            parts, _MonthlyNote, who=who,
        )
        if res is None:
            continue
        if _hash_sources(vault, (card.rel for card in absorbed)) != {
            card.rel: current[card.rel] for card in absorbed
        }:
            continue
        rel = existing.rel if existing else mlayout.rel_path(mlayout.WEEK, week_label)
        previous.update({card.rel: current[card.rel] for card in absorbed})
        if not await _write_digest_card(
            app, rel,
            name=f"weekly-{week_label}",
            description=str(res.get("description") or f"{week_label} 周记忆"),
            content=str(res["content"]),
            metadata={"weekly": week_label,
                      "absorbed": ",".join(previous),
                      "absorbed_hashes": _format_hashes(previous),
                      "tags": res.get("tags") or [],
                      "title": mlayout.display_title(mlayout.WEEK, week_label)},
        ):
            continue  # 写失败：源卡一张不删，下次重试
        _link_profile_to_card(vault, rel, f"周总结-{week_label}")
        deleted += await _delete_absorbed(app, vault, absorbed, who=who, digest_rel=rel,
                                          expected_hashes=current)
        logger.info(f"[memory-janitor] {week_label} 周聚合完成（吸收 {len(absorbed)} 张日总结）")
        done += 1
    return done, deleted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _update_profile(app, vault: str, day_dir: str, day_path: str, notes: list) -> Optional[bool]:
    """把当日记忆卡中的持久信息合并进 用户画像.md，并落 画像->当日总结 auto 边。

    失败返回 None，日卡保留等待重试；无持久属性更新返回 False。
    """
    parts = []
    for fn in sorted(notes):
        try:
            with open(os.path.join(day_path, fn), encoding="utf-8") as f:
                parts.append(f"## 记忆卡片: {fn}\n{f.read()}")
        except OSError:
            continue
    if not parts:
        return None
    sources = [f"daily/{day_dir}/{fn}" for fn in sorted(notes)]
    try:
        snapshot = _hash_sources(vault, sources)
    except OSError:
        return None
    profile_path = os.path.join(vault, PROFILE_NOTE_NAME)
    existing = ""
    try:
        with open(profile_path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        pass
    original = existing
    updated = False
    tags = []
    try:
        for batch in batch_material(parts, 6000):
            if len(existing) + len(batch) > 18000:
                return None
            res = await _llm_structured(
                system=_PROFILE_SYSTEM,
                user=(f"以下是当前用户画像：\n{existing or '（暂无，首次建立）'}\n\n"
                      f"以下是 {day_dir} 的记忆卡素材，请按要求合并。\n\n{batch}"),
                model_cls=_ProfileUpdate,
            )
            if res is None:
                return None
            if not res.get("has_update"):
                continue
            content = str(res.get("content") or "").strip()
            if not content:
                return None
            existing = re.sub(r"^#\s+[^\n]*\n", "", content, count=1).strip()
            tags = [str(tag).strip() for tag in (res.get("tags") or []) if str(tag).strip()]
            updated = True
    except Exception as e:
        logger.error(f"[memory-janitor] {day_dir} 画像合并失败（下次重试）: {e}")
        return None
    if _hash_sources(vault, sources) != snapshot:
        return None
    if not updated:
        return False
    if original and os.path.isfile(profile_path):
        with open(profile_path, encoding="utf-8") as handle:
            if handle.read() != original:
                return None
    _write_profile_file(profile_path, existing, tags)
    # 画像直写磁盘，绕过 ReME 的 write job：立刻同步进检索索引，
    # 否则 memory_search 要等 watch 轮询（且轮询默认不落盘）才能看到新画像
    if not await sync_memory_index(app, vault, [PROFILE_NOTE_NAME], who="画像合并"):
        return None
    logger.info(f"[memory-janitor] {day_dir} 画像已更新 -> {PROFILE_NOTE_NAME}")
    return True


def _write_profile_file(profile_path: str, content: str, tags: list) -> None:
    """整文件重建画像卡。直写而非 ReME write job：watcher 按 vault_path upsert，
    doc id 稳定，画像上的 auto 边不会因文件重建丢失。"""
    tag_lines = "\n".join(f"- {t}" for t in (tags or ["用户画像"]))
    text = (
        "---\n"
        "name: 用户画像\n"
        "description: 用户持久信息画像（明确要求记住/偏好与习惯/背景）\n"
        f"tags:\n{tag_lines}\n"
        f"updated: '{datetime.now().date().isoformat()}'\n"
        "---\n\n"
        "# 用户画像\n\n"
        f"{content}\n"
    )
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(profile_path),
                                         prefix=".profile-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, profile_path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _link_profile_to_card(vault: str, digest_rel: str, dst_label: str) -> None:
    """同步索引画像与目标总结卡，落 画像->总结卡 的 kind='auto' 边。

    重索引只清非 auto 边，auto 边长期留存，图谱提炼链不依赖临时文件。
    digest_rel 是总结卡的 vault 相对路径（digest/day/2026-09-04.md /
    digest/week/weekly-2026-W36.md / digest/month/monthly-2026-09.md），
    dst_label 是边上显示的目标名。路径布局由 memory_layout 统一负责。
    """
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase, KnowledgeLink
    from app.services.knowledge.markdown_indexer import MarkdownIndexer
    vault_abs = os.path.abspath(vault)
    # 画像卡不存在（该用户还没有画像）时直接跳过，不报错
    profile_full = os.path.join(vault_abs, PROFILE_NOTE_NAME)
    if not os.path.exists(profile_full):
        return
    # 迁移窗口内容器可能还留着旧扁平布局的同名卡，按 key 兜一次真实位置
    hit = mlayout.classify(digest_rel)
    card = mlayout.find_card(vault_abs, hit[0], hit[1]) if hit else None
    digest_full = card.abs if card else os.path.join(vault_abs, *digest_rel.split("/"))
    db = SessionLocal()
    try:
        kb = None
        for k in db.query(KnowledgeBase).filter(KnowledgeBase.scope == "personal").all():
            if k.vault_dir and os.path.normcase(os.path.abspath(k.vault_dir)) == os.path.normcase(vault_abs):
                kb = k
                break
        if kb is None:
            return
        idx = MarkdownIndexer(db)
        profile_id = idx.index_file(kb, profile_full)
        digest_id = idx.index_file(kb, digest_full)
        if not profile_id or not digest_id:
            db.commit()
            return
        from app.services.knowledge.index_jobs import enqueue
        enqueue(db, kb.id, profile_id)
        enqueue(db, kb.id, digest_id)
        exists = db.query(KnowledgeLink).filter(
            KnowledgeLink.kb_id == kb.id, KnowledgeLink.src_id == profile_id,
            KnowledgeLink.dst_id == digest_id, KnowledgeLink.kind == "auto",
        ).first()
        if exists is None:
            db.add(KnowledgeLink(
                kb_id=kb.id, src_id=profile_id, dst_id=digest_id,
                dst_name=dst_label, kind="auto",
                anchor=None, context_snippet="画像吸收总结卡",
            ))
        db.commit()
    except Exception as e:
        logger.error(f"[memory-janitor] 画像 auto 边写入失败 {digest_rel}: {e}")
    finally:
        db.close()


async def _compact_profile(app, vault: str) -> bool:
    """每周清洗：压缩画像（合并重复、偏好演变压成最新状态+一句概述、清除任务类内容）。

    红线：用户明确要求记住的条目只合并不删（约束写在 _COMPACT_SYSTEM 提示词里）。
    失败只记日志、返回 False，不阻塞 janitor 主流程。
    """
    profile_path = os.path.join(vault, PROFILE_NOTE_NAME)
    try:
        with open(profile_path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        return False
    if not existing.strip():
        return False
    try:
        res = await _llm_structured(
            system=_COMPACT_SYSTEM,
            user=f"以下是当前用户画像，请按要求清洗压缩。\n\n{existing}",
            model_cls=_ProfileCompact,
        )
    except Exception as e:
        logger.error(f"[memory-janitor] 画像清洗失败（下次重试）: {e}")
        return False
    if not res or not res.get("changed") or not str(res.get("content") or "").strip():
        return False
    content = str(res["content"]).strip()
    content = re.sub(r"^#\s+[^\n]*\n", "", content, count=1).strip()
    tags = [str(t).strip() for t in (res.get("tags") or []) if str(t).strip()]
    _write_profile_file(profile_path, content, tags)
    await sync_memory_index(app, vault, [PROFILE_NOTE_NAME], who="画像清洗")
    logger.info(f"[memory-janitor] 画像每周清洗完成 -> {PROFILE_NOTE_NAME}")
    return True


async def _llm_structured(system: str, user: str, model_cls) -> Optional[dict]:
    """Direct structured-output call on the platform chat model."""
    from app.agents.model_factory import get_model
    model = get_model(settings.MODEL_NAME)
    res = await model.generate_structured_output(
        [SystemMsg(name="system", content=system),
         UserMsg(name="user", content=user)],
        structured_model=model_cls,
    )
    if res is None:
        return None
    content = getattr(res, "content", None)
    return dict(content) if content else None


async def _safe_delete(app, path: str) -> bool:
    """删除 vault 内的文件/目录。True = 确认已不在（删掉了，或本来就不存在）。

    失败只记日志、返回 False：清理是尽力而为，绝不该打断 janitor 的其余步骤；
    调用方用返回值决定是否计数，不靠异常。
    """
    try:
        resp = await _run_app_job_in_thread(app, "delete", path=path)
        if getattr(resp, "success", True) is False:
            # "not found" is fine (already cleaned); log anything else
            ans = str(getattr(resp, "answer", "") or "")
            if "not found" in ans.lower():
                return True
            logger.error(f"[memory-janitor] 删除失败 {path}: {ans}")
            return False
        return True
    except Exception as e:
        logger.warning(f"[memory-janitor] 删除异常 {path}: {e}")
        return False


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_date(s: str):
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return None


_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _frontmatter(raw: str) -> dict:
    m = _FM_RE.match(raw)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            # ReMe 写出的 YAML 值带引号（如 session_id: '75'）；不去引号会
            # 导致 session_id 比对永远失败，同一会话被重复提炼
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1].strip()
            out[k.strip()] = v
    return out


def _collect_session_ids(day_path: str, notes: list) -> set:
    sids = set()
    for fn in notes:
        try:
            with open(os.path.join(day_path, fn), encoding="utf-8") as f:
                sid = str(_frontmatter(f.read()).get("session_id") or "").strip()
            if sid:
                sids.add(sid)
        except OSError:
            continue
    return sids


def _session_referenced_elsewhere(vault: str, session_id: str) -> bool:
    """True if any remaining daily note still references this session_id."""
    daily_root = os.path.join(vault, "daily")
    if not os.path.isdir(daily_root):
        return False
    for root, _dirs, files in os.walk(daily_root):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    if str(_frontmatter(f.read()).get("session_id") or "") == session_id:
                        return True
            except OSError:
                continue
    return False


def _rmdir_if_empty(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass
