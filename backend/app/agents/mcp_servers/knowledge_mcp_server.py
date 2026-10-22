"""知识库 MCP Server（stdio）。

把平台知识库包装成 MCP 工具，任何 MCP 客户端（Claude Desktop、
Codex CLI、Cursor 等）配置后即可直接检索/阅读知识库——知识库从
"平台内部设施"升级为"生态公民"，这是集成方式思路①的落地。

工具列表：
  search_knowledge    混合检索（向量+词面+RRF），返回带引用的片段
  read_document       按 document_id 读原文全文（替身 md）
  list_knowledge_bases 列出可用知识库（名称/范围/文档数）

运行方式（MCP 客户端配置示例）：
  {"mcpServers": {"kb": {
      "command": "python",
      "args": ["<backend>/app/agents/mcp_servers/knowledge_mcp_server.py"],
      "env": {"DATABASE_URL": "...", "KB_MCP_TOKEN": "..."}
  }}}

安全模型（与 email_mcp_server 一致的思路）：
- 只读工具：不提供写入/删除（知识库写入走平台正式管道，有审计）
- 鉴权：KB_MCP_TOKEN 必须是有效 JWT（由平台签发），工具按该用户
  的权限检索（个人库/共享库可见性在 service 层强制，与 Agent 的
  SearchKnowledge 工具同一条权限链）
- stdio 子进程默认白名单环境，需要 DATABASE_URL / KB_MCP_TOKEN
"""

import os
import sys

# stdio 方式运行时保证 import app.* 可用（backend 目录入 sys.path）
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)

import asyncio

# MCP SDK 双版本兼容：1.x 是 FastMCP（mcp.server.fastmcp），
# 2.x 改名 MCPServer（mcp.server.mcpserver）。两者 @mcp.tool() /
# mcp.run(transport="stdio") 用法一致，仅导入路径不同。
try:
    from mcp.server.fastmcp import FastMCP as _MCP
except Exception:
    from mcp.server.mcpserver import MCPServer as _MCP

mcp = _MCP("knowledge", instructions="企业知识库检索：混合检索+文档阅读，只读。")

_TOKEN = (os.environ.get("KB_MCP_TOKEN") or "").strip()
_USER_ID = os.environ.get("KB_MCP_USER_ID", "").strip()


def _current_user():
    """从 JWT 解出用户（与平台同一套校验链，个人/共享库权限在下层强制）。"""
    from app.core.security import decode_token
    from app.database import SessionLocal
    from app.models.user import User
    payload = decode_token(_TOKEN) if _TOKEN else None
    if not payload:
        return None, None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload.get("sub") or 0)).first()
        return db, user
    except Exception:
        db.close()
        return None, None


def _svc(db):
    from app.services.knowledge_service import KnowledgeService
    return KnowledgeService(db)


@mcp.tool()
def search_knowledge(query: str, top_k: int = 5) -> str:
    """在当前用户可见的知识库中混合检索（向量+词面+RRF+可选rerank）。

    返回带 [cite:文档ID#块序号] 引用标记的片段列表，按相关度排序。
    Args:
        query: 自然语言问题或关键词
        top_k: 返回片段数（1~10，默认 5）
    """
    db, user = _current_user()
    if user is None:
        return "未配置有效凭证（KB_MCP_TOKEN），无法检索。"
    try:
        results = asyncio.run(_svc(db).search(
            str(query), kb_ids=_visible_kb_ids(db, user), top_k=min(int(top_k or 5), 10),
        ))
        if not results:
            return "知识库中未检索到相关内容。"
        lines = [
            f"[cite:{r.get('document_id')}#{r.get('chunk_index', 0)}] "
            f"来源: {r.get('filename') or ('文档#' + str(r.get('document_id')))}"
            f"（相关度 {r.get('score')}）\n{r.get('text')}"
            for r in results
        ]
        return "知识库检索结果：\n\n" + "\n\n".join(lines)
    finally:
        db.close()


@mcp.tool()
def read_document(document_id: int) -> str:
    """按文档 ID 读取知识库笔记的原文（Markdown 替身，含标题/frontmatter）。

    Args:
        document_id: search_knowledge 结果里的文档 ID
    """
    db, user = _current_user()
    if user is None:
        return "未配置有效凭证，无法读取。"
    try:
        from app.models.knowledge import KnowledgeDocument
        doc = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.id == int(document_id)).first()
        if doc is None:
            return f"文档 {document_id} 不存在。"
        path = doc.stored_name
        from app.services.knowledge_service import _read_doc_bytes
        data = _read_doc_bytes(doc)
        if data is None:
            return "原文文件缺失（仅元数据）。"
        text = data.decode("utf-8", errors="ignore")
        if len(text) > 60000:
            text = text[:60000] + "\n…（超长截断，用 search_knowledge 定位段落）"
        return f"# {doc.title or path}\n\n{text}"
    finally:
        db.close()


@mcp.tool()
def list_knowledge_bases() -> str:
    """列出当前用户可见的知识库（id/名称/范围/文档数）。"""
    db, user = _current_user()
    if user is None:
        return "未配置有效凭证。"
    try:
        from app.models.knowledge import KnowledgeBase
        q = db.query(KnowledgeBase).filter(KnowledgeBase.kind != "memory")
        if user.role != "admin":
            from sqlalchemy import or_
            q = q.filter(or_(KnowledgeBase.scope == "shared",
                             KnowledgeBase.created_by == user.id))
        kbs = q.all()
        if not kbs:
            return "没有可见的知识库。"
        return "\n".join(
            f"[{kb.id}] {kb.name}（{'共享' if kb.scope == 'shared' else '个人'}，"
            f"{kb.documents.count() if hasattr(kb, 'documents') else '?'} 篇）"
            for kb in kbs
        )
    finally:
        db.close()


def _visible_kb_ids(db, user) -> list:
    from app.models.knowledge import KnowledgeBase
    q = db.query(KnowledgeBase).filter(KnowledgeBase.kind != "memory")
    if user.role != "admin":
        from sqlalchemy import or_
        q = q.filter(or_(KnowledgeBase.scope == "shared",
                         KnowledgeBase.created_by == user.id))
    return [kb.id for kb in q.all()]


if __name__ == "__main__":
    mcp.run(transport="stdio")
