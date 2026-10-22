"""SearchKnowledge 工具：检索绑定知识库（方案B：含当前用户个人库）。
命中片段后附带 1 跳邻居笔记标题（图谱上下文），让回答更关联。"""
from typing import Any

from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)


# 按会话隔离的引用映射注册表：session_id -> {cite序号: "doc_id#chunk_index"}。
# 工具写入、chat 层读取后弹出；并发会话互不覆盖（原模块级全局 dict 存在竞态）。
import threading
from collections import OrderedDict

_citation_lock = threading.Lock()
_citation_maps: "OrderedDict[int, dict]" = OrderedDict()
_MAX_TRACKED_SESSIONS = 256


def pop_citation_map(session_id) -> dict:
    """读取并移除指定会话的引用映射；无记录返回空 dict。"""
    if session_id is None:
        return {}
    with _citation_lock:
        return _citation_maps.pop(int(session_id), None) or {}


def _store_citation_map(session_id, mapping: dict) -> None:
    """登记一次检索产生的引用映射；超出上限时淘汰最旧会话。"""
    if session_id is None or not mapping:
        return
    with _citation_lock:
        existing = _citation_maps.setdefault(int(session_id), {})
        offset = max(existing, default=0)
        existing.update({offset + index: target for index, target in mapping.items()})
        _citation_maps.move_to_end(int(session_id))
        while len(_citation_maps) > _MAX_TRACKED_SESSIONS:
            _citation_maps.popitem(last=False)


class SearchKnowledge(ToolBase):
    name: str = "SearchKnowledge"

    description: str = (
        "Search the bound knowledge base(s) for document chunks relevant to the "
        "query. Use this BEFORE answering whenever the user's question may relate "
        "to internal documents or the knowledge base content. Returns the top "
        "matching chunks with source filenames and relevance scores, plus the "
        "titles of directly linked neighbor notes (graph context) when available. "
        "Cite the source document when using retrieved content in your answer."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题（自然语言，尽量具体）"},
            "top_k": {"type": "integer", "description": "返回片段数（默认 5，最大 10）", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, kb_ids: list, session_id: int | None = None, user_id: int | None = None):
        super().__init__()
        self._kb_ids = [int(k) for k in (kb_ids or [])]
        self._session_id = session_id
        self._user_id = user_id

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """检索工具只读，始终放行。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="SearchKnowledge is read-only, always allowed.",
        )

    async def call(self, query: str, top_k: int = 5) -> ToolChunk:
        if not self._kb_ids:
            return ToolChunk(content=[TextBlock(text="当前 Agent 未绑定任何知识库。")])
        try:
            from app.database import SessionLocal
            from app.services.knowledge_service import KnowledgeService
            from app.models.knowledge import KnowledgeBase
            db = SessionLocal()
            try:
                if self._user_id is None:
                    return ToolChunk(content=[TextBlock(text="无法确认当前用户，拒绝检索。")])
                kb_ids = [kb.id for kb in db.query(KnowledgeBase).filter(
                    KnowledgeBase.id.in_(self._kb_ids), KnowledgeBase.kind != "memory"
                ).all() if kb.scope == "shared" or kb.created_by == self._user_id]
                results = await KnowledgeService(db).search(
                    query=str(query), kb_ids=kb_ids, top_k=min(int(top_k or 5), 10)
                )
                neighbor_ctx = self._graph_context(db, results, kb_ids)
                entity_ctx = self._entity_context(db, kb_ids, str(query))
            finally:
                db.close()
        except Exception as e:
            return ToolChunk(content=[TextBlock(text=f"知识库检索失败：{e}")])

        if not results:
            return ToolChunk(content=[TextBlock(text="知识库中未检索到相关内容。")])

        lines = []
        for i, r in enumerate(results, 1):
            lines.append(
                f"[cite:{r.get('document_id')}#{r.get('chunk_index', 0)}] 来源: {r.get('filename') or ('文档#' + str(r.get('document_id')))}"
                f"（相关度 {r.get('score')}）\n{r.get('text')}"
            )
        _store_citation_map(
            self._session_id,
            {
                idx: f'{r.get("document_id")}#{r.get("chunk_index", 0)}'
                for idx, r in enumerate(results, 1)
            },
        )
        text = "知识库检索结果：\n\n" + "\n\n".join(lines)
        if neighbor_ctx:
            text += "\n\n【图谱上下文：相关笔记的链接邻居】\n" + neighbor_ctx
        if entity_ctx:
            text += "\n\n【实体图谱：查询涉及的实体在其他文档中的出现】\n" + entity_ctx
        return ToolChunk(content=[TextBlock(text=text)])

    def _entity_context(self, db, kb_ids: list[int], query: str) -> str:
        """实体图谱上下文：查询命中的实体 → 跨文档提及（GraphRAG v2.7）。"""
        try:
            from app.services.knowledge.entity_graph import entity_neighbors
            neighbors = entity_neighbors(db, kb_ids, query)
            if not neighbors:
                return ""
            by_ent: dict[str, list] = {}
            for n in neighbors:
                by_ent.setdefault(f"{n['name']}（{n['type']}）", []).append(n)
            parts = []
            for ent, ms in list(by_ent.items())[:6]:
                docs = "、".join(
                    f"{m['doc_title'] or ('文档#' + str(m['doc_id']))}" for m in ms[:4]
                )
                snip = next((m["snippet"] for m in ms if m.get("snippet")), "")
                line = f"- {ent}：另见于 {docs}"
                if snip:
                    line += f"（如：{snip}）"
                parts.append(line)
            return "\n".join(parts)
        except Exception:
            return ""

    def _graph_context(self, db, results, kb_ids: list[int]) -> str:
        """为命中文档拼 1 跳邻居标题（图谱上下文，仅 vault md 有）。"""
        try:
            from app.models.knowledge import KnowledgeLink, KnowledgeDocument
            doc_ids = [int(r["document_id"]) for r in results
                       if str(r.get("document_id", "")).isdigit()]
            if not doc_ids:
                return ""
            seen = set()
            titles = []
            for did in doc_ids:
                # 出链：src=did
                for l in db.query(KnowledgeLink).filter(
                    KnowledgeLink.src_id == did, KnowledgeLink.kb_id.in_(kb_ids)
                ).all():
                    if l.dst_id:
                        d = db.query(KnowledgeDocument).filter(
                            KnowledgeDocument.id == l.dst_id,
                            KnowledgeDocument.kb_id.in_(kb_ids),
                            KnowledgeDocument.status == "ready",
                        ).first()
                        if d and d.title and d.title not in seen:
                            seen.add(d.title); titles.append(d.title)
                    elif l.dst_name and l.dst_name not in seen:
                        seen.add(l.dst_name); titles.append(f"{l.dst_name}（未创建）")
                # 入链：dst=did
                for l in db.query(KnowledgeLink).filter(
                    KnowledgeLink.dst_id == did, KnowledgeLink.kb_id.in_(kb_ids)
                ).all():
                    d = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.id == l.src_id,
                        KnowledgeDocument.kb_id.in_(kb_ids),
                        KnowledgeDocument.status == "ready",
                    ).first()
                    if d and d.title and d.title not in seen:
                        seen.add(d.title); titles.append(d.title)
            return "、".join(titles[:12])
        except Exception:
            return ""
