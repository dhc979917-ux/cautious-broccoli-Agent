"""一次性迁移脚本：个人记忆图谱修复（去重 + 唯一约束 + 用户画像卡种子）。

背景：
1) 并发索引（多进程 watcher / watcher 与全量重索引重叠）在"先查后插"且无唯一约束的
   情况下产生重复的 knowledge_documents 行（同一 (kb_id, vault_path) 多条），
   导致图谱上同一节点显示多次。
2) 个人记忆库的根笔记（如"用户专业背景"）需要并入规范的用户画像卡 用户画像.md，
   作为图谱中心 hub。

动作：
1) 按 (kb_id, vault_path) 去重文档：保留被链接引用的行（否则最小 id），
   迁移 links 指向，清理多余行及其向量。
2) 建 (kb_id, vault_path) 部分唯一索引，防止未来再出现重复。
3) 对每个个人记忆库：若 用户画像.md 不存在，则把根目录显式笔记内容并入
   用户画像.md（背景与决策分节），删除并入的旧根笔记（文件+文档+链接+向量）。

用法（在 backend 目录下，建议先停后端避免并发写）：
    python migrate_memory_graph_v1.py
"""
import os
import re
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
MARKER_KEY = "memory_graph_v1"
PROFILE_NOTE_NAME = "\u7528\u6237\u753b\u50cf.md"  # "用户画像.md"
MEMORY_KB_PREFIX = "\u6211\u7684\u8bb0\u5fc6-"    # "我的记忆-"

_FM_RE = re.compile(r"^\s*---\s*\n.*?\n---\s*\n", re.DOTALL)


def _dedupe_documents(conn) -> int:
    """合并同一 (kb_id, vault_path) 的重复文档行，返回清理的行数。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT kb_id, vault_path FROM knowledge_documents "
        "WHERE vault_path IS NOT NULL "
        "GROUP BY kb_id, vault_path HAVING COUNT(*) > 1"
    )
    groups = cur.fetchall()
    removed = 0
    for kb_id, vp in groups:
        cur.execute(
            "SELECT id FROM knowledge_documents WHERE kb_id = ? AND vault_path = ? ORDER BY id",
            (kb_id, vp),
        )
        ids = [r[0] for r in cur.fetchall()]
        if len(ids) < 2:
            continue
        # keeper：优先被链接引用的行，否则最小 id
        keeper = None
        for did in ids:
            cur.execute(
                "SELECT 1 FROM knowledge_links WHERE src_id = ? OR dst_id = ? LIMIT 1",
                (did, did),
            )
            if cur.fetchone():
                keeper = did
                break
        if keeper is None:
            keeper = ids[0]
        dups = [i for i in ids if i != keeper]
        for d in dups:
            cur.execute("UPDATE knowledge_links SET src_id = ? WHERE src_id = ?", (keeper, d))
            cur.execute("UPDATE knowledge_links SET dst_id = ? WHERE dst_id = ?", (keeper, d))
        # 重指后可能产生重复边（两条重复文档各连了同一目标）：同键去重
        cur.execute(
            "SELECT kb_id, src_id, dst_id, dst_name, kind, COALESCE(anchor, '') FROM knowledge_links "
            "WHERE src_id = ? GROUP BY kb_id, src_id, dst_id, dst_name, kind, COALESCE(anchor, '') "
            "HAVING COUNT(*) > 1",
            (keeper,),
        )
        for (lkb, lsrc, ldst, lname, lkind, lanchor) in cur.fetchall():
            cur.execute(
                "SELECT id FROM knowledge_links WHERE kb_id = ? AND src_id = ? AND dst_id IS ? "
                "AND dst_name = ? AND kind = ? AND COALESCE(anchor, '') = ? ORDER BY id",
                (lkb, lsrc, ldst, lname, lkind, lanchor),
            )
            lids = [r[0] for r in cur.fetchall()]
            for lid in lids[1:]:
                cur.execute("DELETE FROM knowledge_links WHERE id = ?", (lid,))
        for d in dups:
            cur.execute("DELETE FROM knowledge_documents WHERE id = ?", (d,))
            removed += 1
            _purge_vectors_later(kb_id, d)
    conn.commit()
    return removed


_VECTOR_PURGES: list = []


def _purge_vectors_later(kb_id: int, doc_id: int) -> None:
    _VECTOR_PURGES.append((kb_id, doc_id))


def _purge_vectors_now() -> None:
    """统一清理被删文档的向量（失败仅日志）。"""
    if not _VECTOR_PURGES:
        return
    try:
        from app.services.knowledge.markdown_indexer import _purge_doc_vectors
        for kb_id, doc_id in _VECTOR_PURGES:
            _purge_doc_vectors(kb_id, doc_id)
        time.sleep(3)  # 等待后台清理线程落地
        print(f"[OK] 已提交 {len(_VECTOR_PURGES)} 个文档的向量清理")
    except Exception as e:
        print(f"[WARN] 向量清理失败（可忽略，检索侧影响小）: {e}")
    finally:
        _VECTOR_PURGES.clear()


def _seed_profiles() -> int:
    """把个人记忆库的根目录显式笔记并入 用户画像.md，返回新建画像数。"""
    from datetime import date
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase
    from app.services.knowledge.markdown_indexer import MarkdownIndexer

    created = 0
    db = SessionLocal()
    try:
        kbs = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.scope == "personal",
                KnowledgeBase.name.like(MEMORY_KB_PREFIX + "%"),
            )
            .all()
        )
        for kb in kbs:
            if not kb.vault_dir or not os.path.isdir(kb.vault_dir):
                continue
            profile_path = os.path.join(kb.vault_dir, PROFILE_NOTE_NAME)
            if os.path.exists(profile_path):
                continue
            # 根目录显式笔记（vault_path 不含 "/"）
            idx = MarkdownIndexer(db)
            from app.models.knowledge import KnowledgeDocument
            root_docs = (
                db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.kb_id == kb.id,
                    KnowledgeDocument.vault_path.isnot(None),
                    KnowledgeDocument.is_markdown == True,  # noqa: E712
                )
                .all()
            )
            root_docs = [d for d in root_docs if "/" not in (d.vault_path or "")]
            if not root_docs:
                continue
            parts = []
            for d in root_docs:
                fp = os.path.join(kb.vault_dir, d.vault_path)
                try:
                    with open(fp, encoding="utf-8") as f:
                        body = f.read()
                except OSError:
                    continue
                body = _FM_RE.sub("", body, count=1).strip()
                if body:
                    parts.append(body)
            if not parts:
                continue
            content = (
                "---\n"
                "name: \u7528\u6237\u753b\u50cf\n"
                "description: \u7528\u6237\u6301\u4e45\u4fe1\u606f\u753b\u50cf"
                "\uff08\u504f\u597d/\u660e\u786e\u8981\u6c42\u8bb0\u4f4f/\u53cd\u590d\u63d0\u53ca/\u80cc\u666f\uff09\n"
                "tags:\n- \u7528\u6237\u753b\u50cf\n"
                f"updated: '{date.today().isoformat()}'\n"
                "---\n\n"
                "# \u7528\u6237\u753b\u50cf\n\n"
                "## \u80cc\u666f\u4e0e\u51b3\u7b56\n\n"
                + "\n\n".join(parts)
                + "\n"
            )
            with open(profile_path, "w", encoding="utf-8") as f:
                f.write(content)
            idx.index_file(kb, profile_path)
            # 删除已并入的旧根笔记（文档+链接+向量+文件）
            for d in root_docs:
                idx.remove_file(kb, os.path.join(kb.vault_dir, d.vault_path))
                try:
                    os.remove(os.path.join(kb.vault_dir, d.vault_path))
                except OSError:
                    pass
            db.commit()
            created += 1
            print(f"[OK] kb={kb.id} \u5df2\u5e76\u5165 {len(root_docs)} \u7bc7\u6839\u7b14\u8bb0 -> {PROFILE_NOTE_NAME}")
    finally:
        db.close()
    return created


def migrate():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER_KEY,))
        if cur.fetchone():
            print("[INFO] \u8fc1\u79fb\u5df2\u6267\u884c\u8fc7\uff0c\u8df3\u8fc7")
            return

        removed = _dedupe_documents(conn)
        print(f"[OK] \u6587\u6863\u53bb\u91cd\uff1a\u6e05\u7406 {removed} \u6761\u91cd\u590d\u8bb0\u5f55")

        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_kb_vault_path "
            "ON knowledge_documents(kb_id, vault_path) WHERE vault_path IS NOT NULL"
        )
        conn.commit()
        print("[OK] \u5df2\u5efa (kb_id, vault_path) \u552f\u4e00\u7d22\u5f15")

        cur.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM knowledge_documents "
            "WHERE vault_path IS NOT NULL GROUP BY kb_id, vault_path HAVING COUNT(*) > 1)"
        )
        leftover = cur.fetchone()[0]
        print(f"[CHECK] \u5269\u4f59\u91cd\u590d\u7ec4\u6570: {leftover}")

        cur.execute(
            "INSERT INTO _migrations (key, done_at) VALUES (?, datetime('now'))",
            (MARKER_KEY,),
        )
        conn.commit()
    finally:
        conn.close()

    created = _seed_profiles()
    print(f"[OK] \u65b0\u5efa\u7528\u6237\u753b\u50cf\u5361 {created} \u4e2a")
    _purge_vectors_now()
    print("[DONE] \u8fc1\u79fb\u5b8c\u6210")


if __name__ == "__main__":
    migrate()
