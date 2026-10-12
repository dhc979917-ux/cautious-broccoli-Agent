"""图谱服务：从 documents + links 组装节点/边，支持过滤、ego 图、反链、文件夹树、标签云。社区检测用 Louvain (networkx)。"""
import json
import os
import re
from collections import defaultdict, deque
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, KnowledgeLink
# 总结卡的路径布局（digest/day|week|month，兼容迁移前的扁平 digest/）与显示名
# 统一由 memory_layout 提供，图谱与 janitor 共用一份定义，改目录只改那一处。
from app.agents import memory_layout as mlayout


COMMUNITY_COLORS = [
    '#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F',
    '#EDC948', '#B07AA1', '#FF9DA7', '#9C755F', '#BAB0AC',
]


def _tags(doc: KnowledgeDocument) -> List[str]:
    try:
        return json.loads(doc.tags_json or '[]')
    except Exception:
        return []


class GraphService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # 个人记忆库专属图谱（仅 scope=personal 走此分支；共享库完全走原逻辑）
    # 节点只保留摘要层：根笔记 / 当日索引 / 当日总结 / 周总结 / 月度记忆；
    # 无幽灵节点；mem_kind 供前端渲染固定大小与颜色。
    # ------------------------------------------------------------------
    MEM_KIND_META = {
        "profile":        {"label": "用户画像", "color": "#FFB224"},
        "note":           {"label": "笔记",     "color": "#E5484D"},
        "digest_monthly": {"label": "月总结",   "color": "#8E4EC6"},
        "digest_weekly":  {"label": "周总结",   "color": "#3B82F6"},
        "digest_daily":   {"label": "日总结",   "color": "#59A14F"},
        "daily_index":    {"label": "当日索引", "color": "#E5E7EB"},
    }
    _DAILY_INDEX_RE = re.compile(r"^daily/(\d{4}-\d{2}-\d{2})\.md$")
    # 总结卡层级 -> 图谱节点类型；具体路径识别在 memory_layout.classify 里
    _DIGEST_KIND = {
        mlayout.MONTH: "digest_monthly",
        mlayout.WEEK:  "digest_weekly",
        mlayout.DAY:   "digest_daily",
    }

    def graph(self, kb: KnowledgeBase, tags: Optional[List[str]] = None,
              folder: Optional[str] = None, depth: Optional[int] = None,
              focus: Optional[int] = None, include_orphans: bool = True,
             include_tags: bool = True, max_nodes: int = 2000) -> dict:
        if kb.kind == "memory":
            return self._personal_memory_graph(kb)
        md_docs = self.db.query(KnowledgeDocument).filter(
       KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.is_markdown == True  # noqa: E712
        , KnowledgeDocument.status.notin_(("deleted", "archived"))
        ).all()
        pdf_docs = self.db.query(KnowledgeDocument).filter(
        KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.is_markdown == False,  # noqa: E712
        KnowledgeDocument.status == 'ready',
        ).all()
        # Only show MD notes in graph, not PDF/source files
        docs = md_docs
        if tags:
            tset = {t.lower() for t in tags}
            docs = [d for d in docs if any(x.lower() in tset for x in _tags(d))]
        if folder:
            docs = [d for d in docs if (d.vault_path or '').startswith(folder)]
        doc_ids = {d.id for d in docs}
        id_to_doc = {d.id: d for d in docs}

        links = self.db.query(KnowledgeLink).filter(
            KnowledgeLink.kb_id == kb.id, KnowledgeLink.src_id.in_(doc_ids)
        ).all()
        # Exclude links pointing to soft-deleted docs (keep ghost links where dst_id is None)
        links = [l for l in links if l.dst_id is None or l.dst_id in doc_ids]
        adj: Dict[int, set] = defaultdict(set)
        edge_list = []
        ghosts: Dict[str, dict] = {}
        for l in links:
            src = l.src_id
            if l.dst_id and l.dst_id in doc_ids:
                dst = l.dst_id
                adj[src].add(dst); adj[dst].add(src)
                edge_list.append({'source': src, 'target': dst, 'kind': l.kind,
                                  'anchor': l.anchor, 'snippet': l.context_snippet})
            else:
                gkey = 'g::' + (l.dst_name or '').lower()
                ghosts[gkey] = {'id': gkey, 'name': l.dst_name, 'is_ghost': True}
                adj[src].add(gkey)
                edge_list.append({'source': src, 'target': gkey, 'kind': l.kind,
                                  'anchor': l.anchor, 'snippet': l.context_snippet})

        if focus and focus in doc_ids:
            keep = {focus}
            q = deque([(focus, 0)])
            while q:
                n, d = q.popleft()
                if depth and d >= depth:
                    continue
                for nb in adj.get(n, ()):
                    if nb not in keep:
                        keep.add(nb)
                        q.append((nb, d + 1))
            docs = [id_to_doc[i] for i in keep if i in id_to_doc]
            ghosts = {k: v for k, v in ghosts.items() if k in keep}
            edge_list = [e for e in edge_list if e['source'] in keep and e['target'] in keep]
            doc_ids = {d.id for d in docs}
            id_to_doc = {d.id: d for d in docs}

        deg = defaultdict(int)
        for e in edge_list:
            deg[e['source']] += 1; deg[e['target']] += 1
        if not include_orphans:
            docs = [d for d in docs if deg.get(d.id, 0) > 0]

        nodes = []
        for d in docs:
            nodes.append({
                'id': d.id, 'title': d.title or d.filename, 'filename': d.filename,
                'vault_path': d.vault_path, 'tags': _tags(d), 'category': d.category,
                'is_ghost': False, 'is_pdf': not d.is_markdown, 'degree': deg.get(d.id, 0),
            })
        for g in ghosts.values():
            nodes.append({**g, 'degree': deg.get(g['id'], 0), 'is_ghost': True, 'tags': [], 'category': None})

        tag_edges = []
        if include_tags:
            tcount: Dict[str, int] = defaultdict(int)
            tnodes = {}
            for d in docs:
                for t in _tags(d):
                    tcount[t] += 1
                    tnodes['t::' + t] = {'id': 't::' + t, 'name': t, 'is_tag': True}
            for n in tnodes.values():
                nodes.append({**n, 'degree': 0, 'is_ghost': False, 'tags': [], 'category': None})
            for d in docs:
                for t in _tags(d):
                    tag_edges.append({'source': d.id, 'target': 't::' + t, 'kind': 'tag'})

        if len(nodes) > max_nodes:
            nodes = [n for n in nodes if n.get('degree', 0) > 0][:max_nodes]
            keep_ids = {n['id'] for n in nodes}
            edge_list = [e for e in edge_list if e['source'] in keep_ids and e['target'] in keep_ids]
            tag_edges = [e for e in tag_edges if e['source'] in keep_ids and e['target'] in keep_ids]

        all_edges = edge_list + tag_edges
        communities_data: list = []
        node_community: Dict[str, int] = {}
        community_labels: Dict[int, str] = {}
        if len(nodes) > 1 and all_edges:
            try:
                import networkx as nx
                G_nx = nx.Graph()
                for n in nodes:
                    G_nx.add_node(str(n['id']))
                for e in all_edges:
                    G_nx.add_edge(str(e['source']), str(e['target']))
                comms = nx.community.louvain_communities(G_nx, seed=42, resolution=1.0)
                deg_nx = dict(G_nx.degree())
                for cid, members in enumerate(comms):
                    for m in members:
                        node_community[str(m)] = cid
                    if members:
                        hub = max(members, key=lambda mm: deg_nx.get(mm, 0))
                        hub_node = next((n for n in nodes if str(n['id']) == str(hub)), None)
                        label = (hub_node.get('title') or hub_node.get('name') or f'社区{cid}') if hub_node else f'社区{cid}'
                    else:
                        label = f'社区{cid}'
                    community_labels[cid] = label
                    communities_data.append({
                        'cid': cid,
                        'color': COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)],
                        'label': label,
                        'count': len(members),
                    })
            except Exception:
                pass
        if not communities_data:
            communities_data = [{'cid': 0, 'color': COMMUNITY_COLORS[0], 'label': '全部', 'count': len(nodes)}]
            for n in nodes:
                node_community[str(n['id'])] = 0
        for n in nodes:
            cid = node_community.get(str(n['id']), 0)
            n['community'] = cid
            n['community_name'] = community_labels.get(cid, f'社区{cid}')

        return {
            'nodes': nodes,
            'edges': edge_list + tag_edges,
            'stats': {
                'nodes': len(nodes), 'edges': len(edge_list),
                'orphans': sum(1 for d in docs if deg.get(d.id, 0) == 0),
                'ghosts': len(ghosts), 'tags': len(tcount) if include_tags else 0,
            },
            'communities': communities_data,
        }

    def _personal_memory_graph(self, kb: KnowledgeBase) -> dict:
        """个人记忆库图谱：用户画像（hub）+ 摘要层节点（根笔记/当日索引/当日总结/周总结/月度）+ 提炼链语义边 + 标签，无幽灵节点。"""
        import frontmatter as _fm
        from app.agents.memory import PROFILE_NOTE_NAME  # 延迟导入，避免顶层耦合
        docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id,
            KnowledgeDocument.is_markdown == True,  # noqa: E712
            KnowledgeDocument.status.notin_(("deleted", "archived")),
            KnowledgeDocument.vault_path.isnot(None),
        ).all()
        kept: List[KnowledgeDocument] = []
        kind_of: Dict[int, str] = {}
        display_name: Dict[int, str] = {}
        for d in docs:
            vp = (d.vault_path or "").replace("\\", "/")
            if vp == PROFILE_NOTE_NAME:
                kind, name = "profile", "用户画像"
            elif "/" not in vp:
                kind = "note"  # 根目录显式笔记（WriteNote 产物）
                name = d.title or os.path.splitext(d.filename or vp)[0]
            else:
                m = self._DAILY_INDEX_RE.match(vp)
                if m:
                    kind, name = "daily_index", "当日索引"
                else:
                    hit = mlayout.classify(vp)
                    if hit is None:
                        continue  # daily 单卡/会话流水等不入图
                    level, key = hit
                    kind, name = self._DIGEST_KIND[level], mlayout.display_title(level, key)
            kept.append(d)
            kind_of[d.id] = kind
            display_name[d.id] = name
        doc_ids = {d.id for d in kept}
        # 只保留入图节点之间的 wikilink：ghost 链接（dst_id=None）与
        # 指向图外节点（daily 单卡等）的链接整体丢弃
        edge_list = []
        adj: Dict[int, set] = defaultdict(set)
        if doc_ids:
            links = self.db.query(KnowledgeLink).filter(
                KnowledgeLink.kb_id == kb.id,
                KnowledgeLink.src_id.in_(doc_ids),
            ).all()
            for l in links:
                if l.dst_id in doc_ids:
                    adj[l.src_id].add(l.dst_id); adj[l.dst_id].add(l.src_id)
                    edge_list.append({'source': l.src_id, 'target': l.dst_id,
                                      'kind': l.kind, 'anchor': l.anchor,
                                      'snippet': l.context_snippet})
        # 语义边：当日索引页 -> 当日提炼（distilled_from），7天提炼 -> 月度（absorbed）
        if kb.vault_dir:
            by_vp = {(d.vault_path or "").replace("\\", "/"): d for d in kept}
            for d in kept:
                kind = kind_of[d.id]
                # 提炼链：周/月总结经 absorbed frontmatter 连回被吸收的 digest。
                # 当日索引次日消化后即删，不再作为边锚点（画像->日总结由 DB auto 边承载）
                if kind not in ("digest_weekly", "digest_monthly"):
                    continue
                try:
                    meta = _fm.load(os.path.join(kb.vault_dir, d.vault_path)).metadata or {}
                except Exception:
                    continue
                snip = '周聚合' if kind == "digest_weekly" else '月度聚合'
                for part in str(meta.get("absorbed") or "").split(","):
                    # absorbed 历史上有三种写法（裸文件名 / 旧扁平路径 / 新层级路径），
                    # 候选顺序交给 memory_layout。源卡被上层吸收后已删除，候选全部落空、
                    # 这条边自然消失——吸收后图谱只保留 画像->周/月 的 DB auto 边。
                    src = next((by_vp[c] for c in mlayout.absorbed_candidates(part)
                                if c in by_vp), None)
                    if src is not None:
                        adj[src.id].add(d.id); adj[d.id].add(src.id)
                        edge_list.append({'source': src.id, 'target': d.id, 'kind': 'auto',
                                          'anchor': None, 'snippet': snip})
        # 标签节点/边：记忆卡 frontmatter tags -> t::<tag> 节点（补齐标签显示）
        tag_names = set()
        for d in kept:
            for t in _tags(d):
                if t:
                    tag_names.add(t)
                    adj[d.id].add('t::' + t)
                    edge_list.append({'source': d.id, 'target': 't::' + t,
                                      'kind': 'tag', 'anchor': None, 'snippet': None})
        deg = defaultdict(int)
        for e in edge_list:
            deg[e['source']] += 1; deg[e['target']] += 1
        kind_order = list(self.MEM_KIND_META.keys())
        nodes = []
        for d in kept:
            kind = kind_of[d.id]
            nodes.append({
                'id': d.id, 'title': display_name[d.id], 'filename': d.filename,
                'vault_path': d.vault_path, 'tags': _tags(d), 'category': d.category,
                'is_ghost': False, 'is_pdf': False, 'degree': deg.get(d.id, 0),
                'mem_kind': kind,
                'community': kind_order.index(kind),
                'community_name': self.MEM_KIND_META[kind]['label'],
            })
        for t in sorted(tag_names):
            nodes.append({'id': 't::' + t, 'title': t, 'filename': None, 'vault_path': None,
                          'tags': [], 'category': None, 'is_ghost': False, 'is_pdf': False,
                          'is_tag': True, 'degree': deg.get('t::' + t, 0),
                          'mem_kind': None, 'community': None, 'community_name': None})
        communities_data = [
            {'cid': kind_order.index(k), 'color': meta['color'], 'label': meta['label'],
             'count': sum(1 for n in nodes if n['mem_kind'] == k)}
            for k, meta in self.MEM_KIND_META.items()
            if any(n['mem_kind'] == k for n in nodes)
        ]
        return {
            'nodes': nodes,
            'edges': edge_list,
            'stats': {'nodes': len(nodes), 'edges': len(edge_list),
                      'orphans': sum(1 for n in nodes if n['degree'] == 0),
                      'ghosts': 0, 'tags': len(tag_names)},
            'communities': communities_data,
        }

    def backlinks(self, doc_id: int) -> List[dict]:
        links = self.db.query(KnowledgeLink).filter(KnowledgeLink.dst_id == doc_id).all()
        out = []
        for l in links:
            src = self.db.query(KnowledgeDocument).filter(KnowledgeDocument.id == l.src_id).first()
            out.append({
                'src_id': l.src_id, 'src_title': src.title if src else None,
                'src_vault_path': src.vault_path if src else None,
                'kind': l.kind, 'anchor': l.anchor, 'snippet': l.context_snippet,
            })
        return out

    def tree(self, kb: KnowledgeBase) -> dict:
        docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.is_markdown == True,  # noqa: E712
            KnowledgeDocument.status != "archived",
        ).all()
        root = {'name': '', 'children': {}}
        for d in docs:
            parts = (d.vault_path or d.filename).split('/')
            node = root
            for i, p in enumerate(parts):
                is_leaf = (i == len(parts) - 1)
                if is_leaf:
                    node['children'][p] = node['children'].get(p) or {'name': d.title or p, 'doc_id': d.id, 'leaf': True, 'status': d.status}
                else:
                    node['children'].setdefault(p, {'name': p, 'children': {}})
                    node = node['children'][p]
        return self._serialize(root)

    def _serialize(self, node: dict) -> dict:
        ch = node.get('children')
        if not ch:
            return {'name': node['name'], 'doc_id': node.get('doc_id'), 'status': node.get('status')}
        return {'name': node['name'], 'children': [self._serialize(v) for v in ch.values()]}

    def tags(self, kb: KnowledgeBase) -> List[dict]:
        docs = self.db.query(KnowledgeDocument).filter(
            KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.is_markdown == True,  # noqa: E712
            KnowledgeDocument.status.notin_(("deleted", "archived")),
        ).all()
        c: Dict[str, int] = defaultdict(int)
        for d in docs:
            for t in _tags(d):
                c[t] += 1
        return [{'tag': k, 'count': v} for k, v in sorted(c.items(), key=lambda x: -x[1])]
