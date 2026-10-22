"""实体抽取与实体图谱（GraphRAG 实体级，v2.7）。

与文档级图谱（knowledge_links，边=笔记间 wikilink）互补：
节点从"笔记"细化到"领域事物"（设备/部件/报警代码/工具/药剂/部门），
同一个实体跨文档连通——"VN卡盘"在 EP 手册、EP01 宕机报告、保养
规范里被抽成同一个节点。

管道位置：文档转换/索引完成后触发（LLM 抽取），或无网关时走本地
规则抽取（设备编号/报警代码的形态规律强，正则命中率可观）。

存储（学 knowledge_links 惯例，编号迁移建表）：
  knowledge_entities:  id, kb_id, name(唯一), type, mention_count
  entity_mentions:      id, kb_id, entity_id, doc_id, snippet
                       （一篇文档提及一个实体 = 一行，重复提及计数）

检索融合：实体邻居上下文（_entity_context）挂进现有检索出口——
命中块的实体 → 该实体的其他提及文档 → 作为额外上下文喂给 Agent。
"""

import json
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------- 实体类型体系（设备域） ----------
ENTITY_TYPES = {
    "equipment": "设备（整机/机台，如 EP01、DDSG、Slug Etcher）",
    "component": "部件（卡盘/机械臂/泵/传感器等）",
    "alarm": "报警/故障代码（如 VN 卡盘打开错误、C/T OVER）",
    "tool": "工具/耗材（扭矩扳手/无尘布/清洗剂等）",
    "material": "物料/药剂（TPU-5000/HF 等）",
    "process": "工艺/操作（PM 保养/换液/校准等）",
    "person": "人员/部门",
    "metric": "参数/指标（拉伸强度/温度/压力等）",
}

# ---------- LLM 抽取提示词 ----------
EXTRACT_PROMPT = """你是设备知识库的实体抽取器。从下面的文本中抽取领域实体，输出 JSON（不要代码块围栏）：
{{"entities": [{{"name": "实体标准名（简短，去修饰）", "type": "类型代码", "snippet": "实体出现的上下文片段（原文截取，≤60字"}}]}}

类型代码只能取：{type_codes}（含义：{type_desc}）

规则：
- 只抽领域实体，不抽普通名词（"设备""问题""方法"不算）
- 设备编号如 EP01/PL#40/2CPT16 是 equipment
- 报警原文如 "LOADING HAND VACUUM OFF" 是 alarm
- 同一实体只出现一次（取最标准写法）
- 数量上限 15 个，按重要性排序（出现次数多/与故障处理相关的优先）
- 无实体时输出 {{"entities": []}}

文本：
{text}"""


def _chunk_for_extraction(text: str, max_chars: int = 3000) -> List[str]:
    """抽取用切片：按段落聚合，超长硬切（抽取不需要精细边界）。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = []
    cur = []
    cur_len = 0
    for para in text.split("\n"):
        add = len(para) + 1
        if cur and cur_len + add > max_chars:
            parts.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(para)
        cur_len += add
    if cur:
        parts.append("\n".join(cur))
    return parts


# ---------- 本地规则抽取（无网关降级） ----------
# 设备编号：EP01 / PL#40 / 2CPT16 / DSP#18 / 1GRO08 / DDSG#07 等形态
_RE_EQUIPMENT = re.compile(
    r"\b((?:EP|PL|DSP|DSPC|DDSG|FP|FPC|PCS|WS|WS2|Lap|LTA|ASC|APM|SP5XP|SPX|"
    r"2C[EFIP]|1[CGPS]|SG|DFG|IG|GRO|PDS|SDD|SDG|SCT|SST|CPT|CEI|CFM|CPI|"
    r"EBFIS|SLA|SWE|SWC|VI|CPC|PEC|PEP|SG)\s?#?\s?\d{1,3})\b",
    re.IGNORECASE,
)
# 报警代码：大写字母短语含 OFF/ON/OVER/ERROR/ALARM 或 C/T OVER 形态
_RE_ALARM = re.compile(
    r"([A-Z][A-Z0-9 /&\-]{3,40}?(?:OFF|ON|OVER|ERROR|ALARM|FAIL(?:URE)?|TIMEOUT|ABNORMAL)[A-Z0-9 /&\-]{0,10})"
)
# 部件词典（设备域高频，可按需扩充）
_COMPONENT_LEXICON = [
    "卡盘", "机械臂", "机器人", "真空泵", "传感器", "机械手", "上片手",
    "砂轮", "抛盘", "载物台", "加热器", "超声波发生器", "臭氧机",
    "马达", "电机", "气缸", "阀门", "电磁阀", "过滤器", "轴承",
    "主轴", "升降轴", "驱动轴", "影像系统", "对位系统", "浓度计",
]


def extract_entities_local(text: str) -> List[dict]:
    """规则抽取：设备编号 + 报警形态 + 部件词典。零成本降级路。"""
    found = {}
    text = text or ""

    def _add(name, etype, snippet):
        key = name.strip().upper() if etype == "equipment" else name.strip()
        if not key or key in found:
            if key in found:
                found[key]["count"] += 1
            return
        found[key] = {"name": name.strip(), "type": etype,
                      "snippet": snippet.strip()[:60], "count": 1}

    for m in _RE_EQUIPMENT.finditer(text):
        name = re.sub(r"\s", "", m.group(1)).upper()
        start = max(0, m.start() - 20)
        _add(name, "equipment", text[start:m.end() + 20])
    for m in _RE_ALARM.finditer(text):
        name = " ".join(m.group(1).split())
        if 4 <= len(name) <= 45:
            start = max(0, m.start() - 20)
            _add(name, "alarm", text[start:m.end() + 20])
    for comp in _COMPONENT_LEXICON:
        idx = text.find(comp)
        if idx != -1:
            start = max(0, idx - 20)
            _add(comp, "component", text[start:idx + len(comp) + 20])
    ents = sorted(found.values(), key=lambda e: -e["count"])[:15]
    return [{"name": e["name"], "type": e["type"], "snippet": e["snippet"]} for e in ents]


async def extract_entities_llm(text: str) -> Optional[List[dict]]:
    """LLM 抽取（网关可用时）。失败返回 None 走本地降级。"""
    from app.config import settings
    if not (settings.AI_GATEWAY_API_KEY and settings.AI_GATEWAY_BASE_URL):
        return None
    import httpx
    entities = []
    for i, part in enumerate(_chunk_for_extraction(text)):
        if i >= 8:  # 超长文档截断，控制成本
            break
        prompt = EXTRACT_PROMPT.format(
            type_codes="/".join(ENTITY_TYPES.keys()),
            type_desc="；".join(f"{k}={v}" for k, v in ENTITY_TYPES.items()),
            text=part[:3000],
        )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    settings.AI_GATEWAY_BASE_URL.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"},
                    json={"model": settings.MODEL_NAME,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.1},
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                m = re.search(r"\{.*\}", raw, re.S)
                if not m:
                    continue
                data = json.loads(m.group(0))
                for e in (data.get("entities") or [])[:15]:
                    name = str(e.get("name") or "").strip()
                    etype = str(e.get("type") or "").strip()
                    if name and etype in ENTITY_TYPES:
                        entities.append({
                            "name": name[:80],
                            "type": etype,
                            "snippet": str(e.get("snippet") or "")[:60],
                        })
        except Exception as e:
            logger.warning(f"[实体抽取] LLM 第{i}片失败: {e}")
    # 去重（同名保首个）
    seen = set()
    out = []
    for e in entities:
        if e["name"].upper() not in seen:
            seen.add(e["name"].upper())
            out.append(e)
    return out or None


async def extract_entities(text: str) -> List[dict]:
    """主入口：LLM 优先，失败/无网关走本地规则。永不抛异常。"""
    try:
        result = await extract_entities_llm(text)
        if result:
            return result
    except Exception as e:
        logger.warning(f"[实体抽取] LLM 路径异常，走本地: {e}")
    return extract_entities_local(text or "")


# ---------- 入库 ----------

def save_entities(db, kb_id: int, doc_id: int, entities: List[dict]) -> int:
    """幂等写入实体与提及（先清本文档旧提及）。返回写入数。"""
    from sqlalchemy import text as _text
    # 惰性建表（编号迁移同款 DDL，幂等；分条执行兼容 SQLite/SQLAlchemy）
    _DDL = [
        """CREATE TABLE IF NOT EXISTS knowledge_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kb_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'component',
            mention_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(kb_id, name))""",
        "CREATE INDEX IF NOT EXISTS idx_ent_kb ON knowledge_entities(kb_id)",
        """CREATE TABLE IF NOT EXISTS entity_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kb_id INTEGER NOT NULL,
            entity_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL,
            snippet TEXT,
            UNIQUE(entity_id, doc_id))""",
        "CREATE INDEX IF NOT EXISTS idx_ment_kb_doc ON entity_mentions(kb_id, doc_id)",
        "CREATE INDEX IF NOT EXISTS idx_ment_ent ON entity_mentions(entity_id)",
    ]
    for ddl in _DDL:
        db.execute(_text(ddl))
    db.commit()
    # 清本文档旧提及（文档重转换时）
    db.execute(_text(
        "DELETE FROM entity_mentions WHERE kb_id=:kb AND doc_id=:doc"
    ), {"kb": kb_id, "doc": doc_id})
    written = 0
    for e in entities[:30]:
        row = db.execute(_text(
            "SELECT id FROM knowledge_entities WHERE kb_id=:kb AND name=:name"
        ), {"kb": kb_id, "name": e["name"]}).fetchone()
        if row:
            ent_id = row[0]
            db.execute(_text(
                "UPDATE knowledge_entities SET mention_count=mention_count+1 "
                "WHERE id=:id"
            ), {"id": ent_id})
        else:
            r = db.execute(_text(
                "INSERT INTO knowledge_entities (kb_id, name, type, mention_count) "
                "VALUES (:kb, :name, :type, 1)"
            ), {"kb": kb_id, "name": e["name"], "type": e["type"]})
            ent_id = r.lastrowid
        try:
            db.execute(_text(
                "INSERT OR IGNORE INTO entity_mentions (kb_id, entity_id, doc_id, snippet) "
                "VALUES (:kb, :ent, :doc, :snip)"
            ), {"kb": kb_id, "ent": ent_id, "doc": doc_id, "snip": e.get("snippet")})
            written += 1
        except Exception:
            pass
    db.commit()
    return written


def entity_neighbors(db, kb_ids: List[int], query: str, limit: int = 8) -> List[dict]:
    """查询词命中的实体 + 该实体在其他文档的提及（跨文档邻居）。

    返回 [{name, type, doc_id, snippet}]——检索融合的原料。
    """
    from sqlalchemy import text as _text
    from app.services.knowledge.search_tokens import lexemes
    tokens = {t.lower() for t in lexemes(query or "") if len(t) >= 2}
    if not tokens or not kb_ids:
        return []
    placeholders = ",".join(str(int(k)) for k in kb_ids)
    try:
        rows = db.execute(_text(
            f"SELECT id, name, type FROM knowledge_entities WHERE kb_id IN ({placeholders})"
        )).fetchall()
    except Exception:
        return []
    # 查询词与实体名做词级交集打分
    scored = []
    for ent_id, name, etype in rows:
        name_tokens = {t.lower() for t in lexemes(name)}
        overlap = len(name_tokens & tokens)
        if overlap >= 1 and len(name_tokens) <= 6:  # 过滤超长名（假实体）
            scored.append((overlap, len(name_tokens), ent_id, name, etype))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for _, _, ent_id, name, etype in scored[:limit]:
        try:
            mentions = db.execute(_text(
                "SELECT m.doc_id, m.snippet, d.title FROM entity_mentions m "
                f"LEFT JOIN knowledge_documents d ON d.id = m.doc_id "
                f"WHERE m.entity_id = :eid AND m.kb_id IN ({placeholders}) LIMIT 5"
            ), {"eid": ent_id}).fetchall()
        except Exception:
            continue
        for doc_id, snippet, title in mentions:
            out.append({"name": name, "type": etype, "doc_id": doc_id,
                        "snippet": snippet or "", "doc_title": title})
    return out[:20]
