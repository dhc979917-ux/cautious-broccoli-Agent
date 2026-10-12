"""WriteNote 工具：Agent 向已绑定的 vault 知识库写入/更新 markdown 笔记。
作用域守卫（IWE 思想）：只能写到 Agent 已绑定且已设 vault_dir 的 KB；路径防越界。
写后自动重索引（图谱边 + 向量）。"""
import os
from typing import Any, Optional

from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.message import TextBlock
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision


def _safe_name(name: str) -> str:
    name = "".join(c for c in (name or "未命名") if c not in '\\/:*?"<>|').strip()
    if not name:
        name = "未命名"
    if not name.lower().endswith(".md"):
        name += ".md"
    # 禁止越界
    if ".." in name or name.startswith(("/", "\\")):
        raise ValueError("非法笔记名")
    return name


def _append_profile(full: str, content: str) -> None:
    """画像卡追加保护：内容进『待整合记录』分节，绝不覆盖既有画像正文。
    待整合条目由 janitor 的画像合并步骤吸收进正式分节。"""
    from datetime import datetime as _dt
    stamp = _dt.now().date().isoformat()
    entry = f"\n### {stamp}\n{(content or '').strip()}\n"
    if os.path.exists(full):
        with open(full, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = (
            "---\n"
            "name: 用户画像\n"
            "description: 用户持久信息画像（明确要求记住/偏好与习惯/背景）\n"
            "tags:\n- 用户画像\n"
            "---\n\n"
            "# 用户画像\n"
        )
    marker = "## 待整合记录"
    if marker in text:
        idx = text.index(marker) + len(marker)
        text = text[:idx] + "\n" + entry + text[idx:]
    else:
        text = text.rstrip() + f"\n\n{marker}\n{entry}"
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


class WriteNote(ToolBase):
    name: str = "WriteNote"

    description: str = (
        "Create or overwrite a markdown note in a bound vault knowledge base. "
        "Use this when the user asks to record/save a note, decision, or summary "
        "into the knowledge base. When the user asks you to remember personal "
        "information (preferences, facts, decisions about themselves), omit kb_id "
        "so it lands in the user's personal memory vault by default. For durable "
        "user facts (explicit 'remember this' requests, broad durable preferences "
        "like code/report style, stable professional background; NOT task or "
        "project records), set name to '用户画像' so the "
        "content is appended to the user's profile card (never overwritten); use "
        "other names for standalone notes. You may only write to vaults already "
        "bound to this agent. Provide a note name and the markdown content "
        "(frontmatter optional). Declare the note name as the impact scope; never "
        "write outside the bound vault."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "笔记名（不含 .md 也可）"},
            "content": {"type": "string", "description": "markdown 正文（可含 frontmatter）"},
            "kb_id": {"type": "integer", "description": "目标知识库 id（不填默认写用户个人记忆库，其次个人库，最后共享库）"},
        },
        "required": ["name", "content"],
    }

    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, kb_ids: list, user_id: Optional[int] = None):
        super().__init__()
        self._kb_ids = [int(k) for k in (kb_ids or [])]
        self._user_id = user_id

    async def check_permissions(self, tool_input: dict[str, Any], context: PermissionContext) -> PermissionDecision:
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="WriteNote allowed within bound vaults.")

    async def call(self, name: str, content: str, kb_id: Optional[int] = None) -> ToolChunk:
        if not self._kb_ids:
            return ToolChunk(content=[TextBlock(text="当前 Agent 未绑定任何知识库，无法写入。")])
        try:
            from app.database import SessionLocal
            from app.models.knowledge import KnowledgeBase
            from app.models.user import User
            from app.core.deps import has_perm
            from app.services.knowledge.markdown_indexer import MarkdownIndexer
            from app.services.knowledge_service import KnowledgeService
            import asyncio
            safe = _safe_name(name)
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == self._user_id).first()
                if user is None:
                    return ToolChunk(content=[TextBlock(text="无法确认当前用户，拒绝写入。")])
                q = db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(self._kb_ids), KnowledgeBase.vault_dir.isnot(None))
                kbs = [kb for kb in q.all() if (
                    kb.created_by == user.id if kb.scope == "personal"
                    else user.role == "admin" or has_perm(user, "kb_manage.kb_update")
                )]
                if not kbs:
                    return ToolChunk(content=[TextBlock(text="没有可写的知识库或权限。")])
                # 默认目标优先级：个人记忆库（"我的记忆-<uid>"）> 其他个人库 > 共享库。
                # 用户说"记住…"而不指定库时，内容必须进记忆库而不是绑定的共享库。
                from app.agents.memory import PROFILE_NOTE_NAME
                def _rank(k):
                    if k.scope == "personal" and k.kind == "memory":
                        return 0
                    if k.scope == "personal":
                        return 1
                    return 2
                kbs = sorted(kbs, key=lambda k: (_rank(k), k.id))
                kb = None
                if kb_id is not None:
                    kb = next((k for k in kbs if k.id == int(kb_id)), None)
                    if kb is None:
                        return ToolChunk(content=[TextBlock(text="指定的知识库不存在或无写入权限。")])
                else:
                    kb = kbs[0]
                from app.config import settings
                if kb.kind == "memory" and settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED:
                    from app.services.memory_governance import queue_task, generate_task, _hash
                    from fastapi import HTTPException
                    source_id = f"note:{kb.id}:{safe}:{_hash(content)[:16]}"
                    try:
                        task = queue_task(db, user.id,
                            [{"id": source_id, "sha256": _hash(content)}],
                            {source_id: content}, source_type="auto_note",
                            method_code="structured",
                            idempotency_key=f"note:{user.id}:{kb.id}:{safe}:{_hash(content)}")
                    except HTTPException:
                        return ToolChunk(content=[TextBlock(text="笔记候选未入队；长期记忆未写入，请联系管理员。")])
                    return ToolChunk(content=[TextBlock(text="记忆候选已提交审核；批准前不会写入个人记忆或索引。")])
                full = os.path.join(kb.vault_dir, safe)
                # 路径守卫：最终路径必须在 vault_dir 内
                if not os.path.abspath(full).startswith(os.path.abspath(kb.vault_dir) + os.sep):
                    return ToolChunk(content=[TextBlock(text="写入路径越界，已拒绝。")])
                if safe == PROFILE_NOTE_NAME and kb.kind == "memory":
                    # 画像卡只追加不覆盖，避免模型整文件重写丢失既有画像
                    _append_profile(full, content)
                else:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(content or "")
                idx = MarkdownIndexer(db)
                doc_id = idx.index_file(kb, full)
                db.commit()
                if doc_id:
                    from app.services.knowledge.index_jobs import enqueue
                    enqueue(db, kb.id, doc_id)
                # 记忆库卡片还要进 ReME 检索索引（memory_search 面）：笔记是直写磁盘的，
                # 不同步就要等 watch 轮询，根级卡（含画像追加）在此期间搜不到
                if kb.kind == "memory":
                    from app.agents.memory import sync_memory_index_for_user
                    await sync_memory_index_for_user(
                        getattr(kb, "created_by", None), [safe], who=f"WriteNote:{safe}"
                    )
                return ToolChunk(content=[TextBlock(text=f"已写入笔记「{safe}」到知识库「{kb.name}」，已排队索引。")])
            finally:
                db.close()
        except Exception as e:
            return ToolChunk(content=[TextBlock(text=f"写入笔记失败：{e}")])
