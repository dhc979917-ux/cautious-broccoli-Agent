"""Obsidian markdown 索引器：解析 frontmatter + wikilink/相对链接/嵌入/标签，
写 knowledge_documents（笔记元数据）与 knowledge_links（边表，含 ghost 节点）。
只读 md 文件、只写 DB，不修改用户文件。向量化由 KnowledgeService.ingest_vault_md 单独负责。"""
import logging

logger = logging.getLogger(__name__)

import os, re, json, hashlib
from typing import Optional, List, Dict, Tuple

import frontmatter
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, KnowledgeLink

WIKILINK_RE = re.compile(r'!?\[\[([^\[\]]+)\]\]')
MDLINK_RE = re.compile(r'(?<!!)\[([^\]]*)\]\(([^)\s]+)\)')
TAG_RE = re.compile(r'(?<![\w#])#([A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff\-/]*)')


def _norm_name(name: str) -> str:
    """归一化链接目标名：取别名前半、去锚点、去 .md、去首尾空白。"""
    name = name.split('|')[0].split('#')[0].strip()
    if name.lower().endswith('.md'):
        name = name[:-3]
    return name.strip().lower()


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _as_tags(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(t).strip() for t in val if str(t).strip()]
    if isinstance(val, str):
        return [t.strip() for t in val.replace(';', ',').split(',') if t.strip()]
    return []


def _snippet(content: str, pos: int, span: int = 40) -> str:
    s = max(0, pos - span)
    e = min(len(content), pos + span)
    return re.sub(r'\s+', ' ', content[s:e]).strip()


class MarkdownIndexer:
    def __init__(self, db: Session):
        self.db = db

    # ---- 公共入口 ----
    def index_vault(self, kb: KnowledgeBase) -> Dict:
        """全量索引 vault：pass1 upsert 文档，pass2 解析链接（含 ghost）。返回统计 + 变更 doc_id。"""
        if not kb.vault_dir or not os.path.isdir(kb.vault_dir):
            return {"indexed": 0, "changed": [], "links": 0}
        changed: List[int] = []
        md_files: List[str] = []
        for root, _, files in os.walk(kb.vault_dir):
            for fn in files:
                if fn.lower().endswith('.md'):
                    md_files.append(os.path.join(root, fn))
        # pass1：upsert 文档
        for path in md_files:
            doc, changed_flag = self._upsert_doc(kb, path)
            if doc and changed_flag:
                changed.append(doc.id)
        # pass2：解析链接（用全量 name map 解析目标）
        name_map = self._build_name_map(kb)
        total_links = 0
        for path in md_files:
            rel = os.path.relpath(path, kb.vault_dir).replace('\\', '/')
            doc = self.db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb.id,
                KnowledgeDocument.vault_path == rel,
                KnowledgeDocument.is_markdown == True,  # noqa: E712
            ).first()
            if not doc:
                continue
            total_links += self._write_links(kb, doc, path, name_map)
        # pass3: cleanup orphans — DB records whose files no longer exist on disk
        existing_paths = set(os.path.relpath(p, kb.vault_dir).replace('\\', '/') for p in md_files)
        orphan_docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id,
            KnowledgeDocument.is_markdown == True,  # noqa: E712
            KnowledgeDocument.vault_path.isnot(None),
        ).all()
        removed_ids = []
        for doc in orphan_docs:
            if doc.vault_path not in existing_paths:
                if self._governed_memory(kb, doc):
                    doc.status = 'archived'
                    removed_ids.append(doc.id)
                    continue
                removed_ids.append(doc.id)
                self.db.query(KnowledgeLink).filter(
                    (KnowledgeLink.src_id == doc.id) | (KnowledgeLink.dst_id == doc.id)
                ).delete(synchronize_session=False)
                self.db.delete(doc)
        self.db.commit()
        if removed_ids:
            from app.services.knowledge.index_jobs import enqueue
            for doc_id in removed_ids:
                enqueue(self.db, kb.id, doc_id, "delete")
        return {"indexed": len(md_files), "changed": changed, "links": total_links}

    def index_file(self, kb: KnowledgeBase, abs_path: str, *, commit: bool = True) -> Optional[int]:
        """单文件增量（watcher 用）：upsert + 重写该文件链接。"""
        if not abs_path.lower().endswith('.md'):
            return None
        doc, _ = self._upsert_doc(kb, abs_path)
        name_map = self._build_name_map(kb)
        self._write_links(kb, doc, abs_path, name_map)
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return doc.id

    def remove_file(self, kb: KnowledgeBase, abs_path: str):
        """文件被删：删文档+其链接+其向量（向量不清会残留孤儿，检索命中已删内容）。"""
        rel = os.path.relpath(abs_path, kb.vault_dir).replace('\\', '/') if kb.vault_dir else None
        doc = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.vault_path == rel
        ).first()
        if doc:
            self.db.query(KnowledgeLink).filter(
                (KnowledgeLink.src_id == doc.id) | (KnowledgeLink.dst_id == doc.id)
            ).delete(synchronize_session=False)
            doc_id, kb_id = doc.id, kb.id
            if self._governed_memory(kb, doc):
                if doc.status == 'archived':
                    return
                doc.status = 'archived'
            else:
                self.db.delete(doc)
            self.db.commit()
            from app.services.knowledge.index_jobs import enqueue
            enqueue(self.db, kb_id, doc_id, "delete")

    def _governed_memory(self, kb, doc):
        from app.config import settings
        if kb.kind != 'memory' or not settings.GOVERNANCE_MEMORY_CANDIDATES_ENABLED:
            return False
        from app.models.memory_governance import MemoryEntry
        return self.db.query(MemoryEntry.id).filter_by(kb_id=kb.id, doc_id=doc.id).first() is not None


    # ---- 内部 ----
    def _upsert_doc(self, kb: KnowledgeBase, path: str) -> Tuple[Optional[KnowledgeDocument], bool]:
        with open(path, 'rb') as source:
            raw = source.read()
        h = _md5(raw)
        rel = os.path.relpath(path, kb.vault_dir).replace('\\', '/')
        # 并发防护：(kb_id, vault_path) 有唯一索引，先查后插在竞态下可能撞唯一约束，
        # 撞了回滚改走更新，避免重复行（见 migrate_memory_graph_v1）
        for _attempt in range(2):
            doc = self.db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.vault_path == rel
            ).first()
            if doc and doc.status == "deleted":
                return doc, False
            if doc and doc.content_hash == h:
                return doc, False
            try:
                post = frontmatter.load(path)
                meta = dict(post.metadata or {})
                content = post.content
            except Exception:
                meta, content = {}, raw.decode('utf-8', errors='ignore')
            title = meta.get('title') or self._first_h1(content) or os.path.splitext(os.path.basename(path))[0]
            # 记忆库当日索引统一显示名（文档列表与图谱一致；该文件次日消化后即删）
            if re.match(r"^daily/\d{4}-\d{2}-\d{2}\.md$", rel):
                title = "当日索引"
            tags = _as_tags(meta.get('tags'))
            tags += [t for t in TAG_RE.findall(content) if t not in tags]
            tags = list(dict.fromkeys(tags))  # 去重保序
            if doc is None:
                doc = KnowledgeDocument(kb_id=kb.id, filename=os.path.basename(path))
                self.db.add(doc)
            doc.filename = os.path.basename(path)
            doc.stored_name = None
            doc.size = len(raw)
            doc.title = title
            doc.vault_path = rel
            doc.frontmatter_json = json.dumps(meta, ensure_ascii=False, default=str)
            doc.tags_json = json.dumps(tags, ensure_ascii=False)
            doc.category = meta.get('category') or doc.category
            doc.content_hash = h
            doc.is_markdown = True
            doc.status = "indexing"
            try:
                self.db.flush()
                return doc, True
            except IntegrityError:
                self.db.rollback()
                continue  # 并发插入已抢先，重试一轮改走更新
        # 两轮都失败：返回已有记录（不阻断调用方）
        doc = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.vault_path == rel
        ).first()
        return doc, False

    @staticmethod
    def _first_h1(content: str) -> Optional[str]:
        m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        return m.group(1).strip() if m else None

    def _build_name_map(self, kb: KnowledgeBase) -> Dict[str, int]:
        """name(小写) -> doc_id：basename(去.md)、title、frontmatter aliases、vault 相对路径都能命中。"""
        m: Dict[str, int] = {}
        docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.is_markdown == True,  # noqa: E712
            KnowledgeDocument.status.notin_(("deleted", "archived")),
        ).all()
        for d in docs:
            base = os.path.splitext(d.filename)[0]
            for key in [base, d.title] + ([d.title] if d.title else []):
                if key:
                    m.setdefault(str(key).strip().lower(), d.id)
            # 路径形式目标（ReME 的当日索引用 [[daily/2026-08-26/xxx.md]] 全路径引用）
            vp = (d.vault_path or "").replace("\\", "/")
            if vp:
                m.setdefault(vp.lower(), d.id)
                if vp.lower().endswith(".md"):
                    m.setdefault(vp[:-3].lower(), d.id)
            try:
                meta = json.loads(d.frontmatter_json or '{}')
                for a in _as_tags(meta.get('aliases')) + _as_tags(meta.get('alias')):
                    m.setdefault(a.strip().lower(), d.id)
            except Exception:
                pass
        return m

    def _write_links(self, kb: KnowledgeBase, doc: KnowledgeDocument, path: str, name_map: Dict[str, int]) -> int:
        with open(path, 'r', encoding='utf-8', errors='ignore') as source:
            content = source.read()
        self.db.query(KnowledgeLink).filter(KnowledgeLink.src_id == doc.id, KnowledgeLink.kind != "auto").delete(synchronize_session=False)
        edges: List[dict] = []
        seen = set()
        for m in WIKILINK_RE.finditer(content):
            raw = m.group(1)
            embed = content[m.start() - 1] == '!' if m.start() > 0 else False
            parts = raw.split('|')
            target = parts[0]
            anchor = None
            if '#' in target:
                target, anchor = target.split('#', 1)
            dst_name = target.strip()
            if not dst_name:
                continue
            key = (dst_name.lower(), 'embed' if embed else 'link', anchor)
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                'dst_name': dst_name, 'kind': 'embed' if embed else 'link',
                'anchor': anchor, 'snip': _snippet(content, m.start()),
            })
        for m in MDLINK_RE.finditer(content):
            target = m.group(2)
            if not target.lower().endswith('.md'):
                continue
            anchor = None
            if '#' in target:
                target, anchor = target.split('#', 1)
            dst_name = os.path.splitext(os.path.basename(target))[0]
            key = (dst_name.lower(), 'link', anchor)
            if key in seen:
                continue
            seen.add(key)
            edges.append({'dst_name': dst_name, 'kind': 'link', 'anchor': anchor,
                          'snip': _snippet(content, m.start())})
        for e in edges:
            dst_id = name_map.get(e['dst_name'].strip().lower())
            if dst_id is None:
                # 路径式目标的兜底：取 basename 再试（[[a/b/xxx.md]] -> xxx）
                bn = os.path.basename(e['dst_name'].replace('\\', '/'))
                if bn.lower().endswith('.md'):
                    bn = bn[:-3]
                dst_id = name_map.get(bn.strip().lower())
            self.db.add(KnowledgeLink(
                kb_id=kb.id, src_id=doc.id, dst_id=dst_id,
                dst_name=e['dst_name'], kind=e['kind'], anchor=e.get('anchor'),
                context_snippet=e['snip'],
            ))
        return len(edges)


def _purge_doc_vectors(kb_id: int, doc_id: int):
    """尽力清理文档向量：后台线程独立事件循环执行，失败仅日志不阻塞索引流程。
    调用方多为 watchdog 回调线程（无事件循环），故不复用调用方 loop。"""
    import asyncio
    import threading

    async def _do():
        from app.services.knowledge_service import get_vector_store, _collection
        store = get_vector_store()
        coll = _collection(kb_id)
        if await store.has_collection(coll):
            await store.delete(coll, str(doc_id))

    def _run():
        try:
            asyncio.run(_do())
        except Exception as e:
            logger.error(f"[indexer] 清理向量失败 doc={doc_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
