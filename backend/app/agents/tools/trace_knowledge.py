"""TraceKnowledge 工具：沿知识图谱多跳溯源。
给定起点文档（doc_id 或标题关键词），返回其 ego 图邻居（出链/入链/双向，
可配置跳数）与反链，支持读取文档全文。用于"顺藤摸瓜"式追溯知识关系：
先用 SearchKnowledge 命中线索，再用本工具沿链接逐跳深入。"""
import os
from typing import Any

from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)

_MAX_CONTENT_CHARS = 4000  # 单文档全文回传的截断上限


class TraceKnowledge(ToolBase):
    name: str = "TraceKnowledge"

    description: str = (
        "Trace the knowledge graph from a starting document to explore related "
        "notes across multiple hops. Use it to follow links/backlinks between "
        "knowledge base documents (root-cause tracing, finding related context). "
        "Provide doc_id (from SearchKnowledge results) or a title query to locate "
        "the start node; set depth (1-3) and direction. Returns the start "
        "document summary, its linked neighbors with doc ids for follow-up "
        "calls, and optionally the full content."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "integer", "description": "起点文档 ID（来自 SearchKnowledge 的 cite 标记）"},
            "query": {"type": "string", "description": "无 doc_id 时按标题/文件名模糊定位起点文档"},
            "depth": {"type": "integer", "description": "遍历跳数（默认 1，最大 3）", "default": 1, "minimum": 1, "maximum": 3},
            "direction": {"type": "string", "enum": ["forward", "backward", "both"], "description": "forward=出链 backward=入链 both=双向（默认）", "default": "both"},
            "read_content": {"type": "boolean", "description": "是否返回起点文档全文（默认 false）", "default": False},
        },
    }

    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, kb_ids: list):
        super().__init__()
        self._kb_ids = [int(k) for k in (kb_ids or [])]

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """只读工具，始终放行（可见性由 kb_ids 白名单保证）。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="TraceKnowledge is read-only, always allowed.",
        )

    async def call(
        self,
        doc_id: int = None,
        query: str = "",
        depth: int = 1,
        direction: str = "both",
        read_content: bool = False,
    ) -> ToolChunk:
        if not self._kb_ids:
            return ToolChunk(content=[TextBlock(text="当前 Agent 未绑定任何知识库。")])
        depth = max(1, min(int(depth or 1), 3))
        direction = direction if direction in ("forward", "backward", "both") else "both"
        try:
            from app.database import SessionLocal
            from app.models.knowledge import KnowledgeBase, KnowledgeDocument
            from app.services.knowledge.graph_service import GraphService
            db = SessionLocal()
            try:
                # ---- 定位起点文档（仅限可访问 KB）----
                doc = None
                if doc_id:
                    doc = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.id == int(doc_id),
                        KnowledgeDocument.kb_id.in_(self._kb_ids),
                        KnowledgeDocument.status == "ready",
                    ).first()
                elif query:
                    like = f"%{str(query).strip()}%"
                    doc = db.query(KnowledgeDocument).filter(
                        KnowledgeDocument.kb_id.in_(self._kb_ids),
                        KnowledgeDocument.status == "ready",
                        (KnowledgeDocument.title.like(like)) | (KnowledgeDocument.filename.like(like)),
                    ).first()
                if doc is None:
                    hint = "未找到该文档。请先用 SearchKnowledge 检索，或换一个标题关键词。"
                    return ToolChunk(content=[TextBlock(text=hint)])
                kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
                if kb is None or kb.id not in self._kb_ids:
                    return ToolChunk(content=[TextBlock(text="无权访问该文档所在知识库。")])

                # ---- ego 图遍历 ----
                graph = GraphService(db).graph(kb, focus=doc.id, depth=depth, include_orphans=False)
                nodes = {n["id"]: n for n in graph.get("nodes", [])}
                edges = graph.get("edges", [])
                backlinks = GraphService(db).backlinks(doc.id)
            finally:
                db.close()
        except Exception as e:
            return ToolChunk(content=[TextBlock(text=f"图谱溯源失败：{e}")])

        lines = [f"起点文档 #{doc.id}: {doc.title or doc.filename}（知识库: {kb.name}）"]
        if doc.category:
            lines.append(f"分类: {doc.category}")
        if doc.vault_path:
            lines.append(f"路径: {doc.vault_path}")

        if read_content:
            content = self._read_doc_content(kb, doc)
            if content:
                lines.append("\n--- 文档全文 ---\n" + content)

        # ---- 邻居（按方向过滤）----
        fwd, bwd = [], []
        for e in edges:
            if e["source"] == doc.id and direction in ("forward", "both"):
                n = nodes.get(e["target"])
                if n:
                    fwd.append((n, e))
            if e["target"] == doc.id and direction in ("backward", "both"):
                n = nodes.get(e["source"])
                if n:
                    bwd.append((n, e))
        if fwd:
            lines.append("\n--- 出链（本文档引用/链接的笔记）---")
            for n, e in fwd[:20]:
                tag = "（未创建）" if n.get("is_ghost") else f"#{n['id']}"
                snippet = f" — {(e.get('snippet') or '')[:80]}" if e.get("snippet") else ""
                lines.append(f"  [{e.get('kind')}] {n.get('title') or n.get('name')} {tag}{snippet}")
        if bwd:
            lines.append("\n--- 入链（引用本文档的笔记）---")
            for n, e in bwd[:20]:
                tag = "（未创建）" if n.get("is_ghost") else f"#{n['id']}"
                snippet = f" — {(e.get('snippet') or '')[:80]}" if e.get("snippet") else ""
                lines.append(f"  [{e.get('kind')}] {n.get('title') or n.get('name')} {tag}{snippet}")
        if backlinks and direction in ("backward", "both"):
            extra = [b for b in backlinks if b.get("src_id")]
            if extra:
                lines.append("\n--- 反链详情 ---")
                for b in extra[:10]:
                    lines.append(f"  #{b['src_id']} {b.get('src_title')}: {(b.get('snippet') or '')[:100]}")
        if not fwd and not bwd:
            lines.append("\n该文档没有图谱链接（孤立节点）。可尝试 SearchKnowledge 换关键词检索。")
        else:
            lines.append("\n提示：可用返回的 #doc_id 作为下一次 TraceKnowledge 的 doc_id 继续溯源。")
        return ToolChunk(content=[TextBlock(text="\n".join(lines))])

    def _read_doc_content(self, kb, doc) -> str:
        """读取 vault md 全文（非 md/非 vault 文档返回说明）。"""
        if not doc.is_markdown or not kb.vault_dir or not doc.vault_path:
            return "（该文档非 vault 笔记，无全文可读；请用 SearchKnowledge 检索其片段）"
        p = os.path.join(kb.vault_dir, doc.vault_path)
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            return f"（读取失败：{e}）"
        if len(raw) > _MAX_CONTENT_CHARS:
            raw = raw[:_MAX_CONTENT_CHARS] + f"\n…[已截断，全文共 {len(raw)} 字符]"
        return raw
