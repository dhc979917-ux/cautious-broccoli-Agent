"""个人记忆库总结卡（digest）的存放布局 —— 全项目单一事实来源。

布局（唯一写入目标）::

    digest/day/<YYYY-MM-DD>.md          日总结（当日记忆卡提炼）
    digest/week/weekly-<YYYY>-W<WW>.md  周总结（已结束 ISO 周的日总结聚合）
    digest/month/monthly-<YYYY-MM>.md   月总结（上月周总结聚合，回退日总结）

历史布局（只读兼容，由 migrate_digest_layout_v1.py 搬迁）::

    digest/<同名文件>.md                三层混放在同一目录

约定：
- 写侧只用 rel_path()/abs_path()，读侧只用 iter_cards()/find_card()/classify()/
  absorbed_candidates()。以后加层级（例如季度）或再改目录，只改本模块。
- 对外路径一律是 vault 相对、posix 分隔，与 KnowledgeDocument.vault_path 及
  ReME 的 workspace 相对路径保持一致。
- 本模块不依赖 DB / ReME / FastAPI，任何层都能安全导入（无循环依赖风险）。
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

DIGEST_ROOT = "digest"

# 三个总结层级；顺序即"细 -> 粗"，聚合与展示都按此序
DAY = "day"
WEEK = "week"
MONTH = "month"
LEVELS: Tuple[str, ...] = (DAY, WEEK, MONTH)

SUBDIR: Dict[str, str] = {
    DAY: f"{DIGEST_ROOT}/day",
    WEEK: f"{DIGEST_ROOT}/week",
    MONTH: f"{DIGEST_ROOT}/month",
}

DateLike = Union[date, datetime, str]


class Card(NamedTuple):
    """一张总结卡的定位信息（key 用于排序/归组，rel 用于 DB 与 ReME）。"""

    level: str
    key: str        # 2026-09-04 / 2026-W36 / 2026-09
    filename: str   # 2026-09-04.md / weekly-2026-W36.md / monthly-2026-09.md
    rel: str        # vault 相对路径（posix）
    abs: str        # 磁盘绝对路径


# ---------------------------------------------------------------------------
# 文件名 / 路径 正则：新布局与旧扁平布局都吃，读侧永不漏卡
# ---------------------------------------------------------------------------
_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    (DAY, re.compile(rf"^{DIGEST_ROOT}/{DAY}/[a-f0-9-]{{36}}/[a-f0-9-]{{36}}/(\d{{4}}-\d{{2}}-\d{{2}})\.md$")),
    (WEEK, re.compile(rf"^{DIGEST_ROOT}/{WEEK}/[a-f0-9-]{{36}}/[a-f0-9-]{{36}}/weekly-(\d{{4}})-W(\d{{2}})\.md$")),
    (MONTH, re.compile(rf"^{DIGEST_ROOT}/{MONTH}/[a-f0-9-]{{36}}/[a-f0-9-]{{36}}/monthly-(\d{{4}}-\d{{2}})\.md$")),
    (DAY, re.compile(rf"^{DIGEST_ROOT}/(?:{DAY}/)?(\d{{4}}-\d{{2}}-\d{{2}})\.md$")),
    (WEEK, re.compile(rf"^{DIGEST_ROOT}/(?:{WEEK}/)?weekly-(\d{{4}})-W(\d{{2}})\.md$")),
    (MONTH, re.compile(rf"^{DIGEST_ROOT}/(?:{MONTH}/)?monthly-(\d{{4}}-\d{{2}})\.md$")),
)

_FN_PATTERNS: Dict[str, re.Pattern] = {
    DAY: re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$"),
    WEEK: re.compile(r"^weekly-(\d{4})-W(\d{2})\.md$"),
    MONTH: re.compile(r"^monthly-(\d{4}-\d{2})\.md$"),
}

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_WEEK_KEY_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTH_KEY_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _norm_rel(path: str) -> str:
    """任意写法的路径 -> vault 相对 posix 路径（去 ./ 与前导 /）。"""
    p = str(path or "").replace("\\", "/").strip()
    while p.startswith("/"):
        p = p[1:]
    return p


# ---------------------------------------------------------------------------
# key 归一化
# ---------------------------------------------------------------------------
def day_key(d: DateLike) -> str:
    """date/datetime/'2026-09-04' -> '2026-09-04'。"""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    m = _DATE_RE.match(str(d).strip())
    if not m:
        raise ValueError(f"非法日 key: {d!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()


def week_key(label: DateLike) -> str:
    """date/datetime/'2026-W36' -> '2026-W36'（ISO 周）。"""
    if isinstance(label, (date, datetime)):
        iso = label.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    s = str(label).strip().upper().replace(" ", "")
    m = _WEEK_KEY_RE.match(s)
    if not m:
        raise ValueError(f"非法周 key: {label!r}")
    return f"{m.group(1)}-W{int(m.group(2)):02d}"


def month_key(label: DateLike) -> str:
    """date/datetime/'2026-09' -> '2026-09'。"""
    if isinstance(label, (date, datetime)):
        return label.strftime("%Y-%m")
    m = _MONTH_KEY_RE.match(str(label).strip())
    if not m:
        raise ValueError(f"非法月 key: {label!r}")
    return f"{m.group(1)}-{int(m.group(2)):02d}"


_KEY_NORMALIZER = {DAY: day_key, WEEK: week_key, MONTH: month_key}


def normalize_key(level: str, key: DateLike) -> str:
    try:
        return _KEY_NORMALIZER[level](key)
    except KeyError:
        raise ValueError(f"未知总结层级: {level!r}") from None


# ---------------------------------------------------------------------------
# 路径构造（写侧唯一入口）
# ---------------------------------------------------------------------------
def filename(level: str, key: DateLike) -> str:
    k = normalize_key(level, key)
    if level == DAY:
        return f"{k}.md"
    if level == WEEK:
        return f"weekly-{k}.md"
    return f"monthly-{k}.md"


def rel_path(level: str, key: DateLike) -> str:
    """新布局的 vault 相对路径，例如 digest/week/weekly-2026-W36.md。"""
    return f"{SUBDIR[level]}/{filename(level, key)}"


def legacy_rel_path(level: str, key: DateLike) -> str:
    """旧扁平布局的 vault 相对路径，例如 digest/weekly-2026-W36.md（只读兼容）。"""
    return f"{DIGEST_ROOT}/{filename(level, key)}"


def reviewed_rel_path(level: str, key: DateLike, task_id: str, item_id: str) -> str:
    if not all(re.fullmatch(r"[a-f0-9-]{36}", identity) for identity in (task_id, item_id)):
        raise ValueError("非法审核任务或条目标识")
    return f"{SUBDIR[level]}/{task_id}/{item_id}/{filename(level, key)}"


def abs_path(vault: str, level: str, key: DateLike) -> str:
    return os.path.join(vault, *rel_path(level, key).split("/"))


def abs_dir(vault: str, level: str) -> str:
    return os.path.join(vault, *SUBDIR[level].split("/"))


def display_title(level: str, key: DateLike) -> str:
    """统一显示名（文档列表与图谱共用）：day-2026-09-04 / week-2026-09-1 / month-2026-09。"""
    k = normalize_key(level, key)
    if level == DAY:
        return f"day-{k}"
    if level == MONTH:
        return f"month-{k}"
    monday = week_monday(k)
    if monday is None:
        return f"week-{k}"
    return f"week-{monday.year:04d}-{monday.month:02d}-{(monday.day - 1) // 7 + 1}"


# ---------------------------------------------------------------------------
# 周/月 时间换算
# ---------------------------------------------------------------------------
def iso_week_label(d: DateLike) -> str:
    """任意日期 -> 所属 ISO 周 key（2026-W36）。"""
    if isinstance(d, str):
        parsed = parse_day(d)
        if parsed is None:
            raise ValueError(f"非法日期: {d!r}")
        d = parsed
    return week_key(d)


def week_monday(label: str) -> Optional[date]:
    try:
        return datetime.strptime(f"{week_key(label)}-1", "%G-W%V-%u").date()
    except ValueError:
        return None


def week_thursday(label: str) -> Optional[date]:
    """ISO 周归月依据：该周的周四一定落在该周所属的 ISO 年内，且月份唯一。"""
    try:
        return datetime.strptime(f"{week_key(label)}-4", "%G-W%V-%u").date()
    except ValueError:
        return None


def parse_day(s: str) -> Optional[date]:
    m = _DATE_RE.match(str(s or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 识别（读侧）
# ---------------------------------------------------------------------------
def classify(vault_path: str) -> Optional[Tuple[str, str]]:
    """vault 相对路径 -> (level, key)；不是总结卡返回 None。新旧布局都识别。"""
    p = _norm_rel(vault_path)
    for level, rx in _PATTERNS:
        m = rx.match(p)
        if not m:
            continue
        if level == DAY:
            return DAY, m.group(1)
        if level == WEEK:
            return WEEK, f"{m.group(1)}-W{m.group(2)}"
        return MONTH, m.group(1)
    return None


def classify_filename(fn: str) -> Optional[Tuple[str, str]]:
    """裸文件名 -> (level, key)；用于解析 absorbed 里只写文件名的历史数据。"""
    base = _norm_rel(fn).rsplit("/", 1)[-1]
    for level, rx in _FN_PATTERNS.items():
        m = rx.match(base)
        if not m:
            continue
        if level == DAY:
            return DAY, m.group(1)
        if level == WEEK:
            return WEEK, f"{m.group(1)}-W{m.group(2)}"
        return MONTH, m.group(1)
    return None


def iter_cards(vault: str, level: str) -> List[Card]:
    """列出某层级的全部总结卡（新目录 + 旧扁平目录），按 key 升序、同 key 去重。

    迁移未跑完时也不会漏卡：同一 key 若两处都在，以新布局为准（旧的是待搬迁残留）。
    """
    found: Dict[str, Card] = {}
    roots = (abs_dir(vault, level), os.path.join(vault, DIGEST_ROOT))
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for fn in entries:
            hit = classify_filename(fn)
            if hit is None or hit[0] != level:
                continue
            key = hit[1]
            full = os.path.join(root, fn)
            if not os.path.isfile(full):
                continue
            rel = _norm_rel(os.path.relpath(full, vault))
            card = Card(level=level, key=key, filename=fn, rel=rel, abs=full)
            prev = found.get(key)
            # 新布局优先；两处都存在时保留新布局那张
            if prev is None or (prev.rel != rel_path(level, key) and rel == rel_path(level, key)):
                found[key] = card
    reviewed = []
    root = abs_dir(vault, level)
    if os.path.isdir(root):
        for directory, children, filenames in os.walk(root, followlinks=False):
            children[:] = [child for child in children if re.fullmatch(r"[a-f0-9-]{36}", child)
                           and not os.path.islink(os.path.join(directory, child))]
            for name in filenames:
                full = os.path.join(directory, name)
                if os.path.islink(full):
                    continue
                relative = _norm_rel(os.path.relpath(full, vault))
                hit = classify(relative)
                if hit and hit[0] == level and relative.count("/") == 4:
                    reviewed.append(Card(level=level, key=hit[1], filename=name, rel=relative, abs=full))
    return sorted([found[key] for key in sorted(found)] + reviewed, key=lambda card: (card.key, card.rel))


def find_card(vault: str, level: str, key: DateLike) -> Optional[Card]:
    """按 key 定位一张已存在的总结卡（新旧布局都找）。"""
    k = normalize_key(level, key)
    for cand in (rel_path(level, k), legacy_rel_path(level, k)):
        full = os.path.join(vault, *cand.split("/"))
        if os.path.isfile(full):
            return Card(level=level, key=k, filename=os.path.basename(full),
                        rel=cand, abs=full)
    return next((card for card in iter_cards(vault, level) if card.key == k), None)


def exists(vault: str, level: str, key: DateLike) -> bool:
    return find_card(vault, level, key) is not None


def absorbed_candidates(entry: str) -> List[str]:
    """周/月卡 frontmatter 的 absorbed 条目 -> 候选 vault 相对路径（按优先级）。

    兼容三种历史写法：裸文件名、旧扁平相对路径、新层级相对路径。
    图谱据此把"提炼链"边连回源卡；源卡已被吸收删除时全部落空，边自然消失
    （设计如此：吸收后只保留 画像->周/月 的 DB auto 边）。
    """
    raw = _norm_rel(entry)
    if not raw:
        return []
    if classify(raw) is not None:
        return [raw]
    base = raw.rsplit("/", 1)[-1]
    hit = classify_filename(base)
    if hit is not None:
        level, key = hit
        return [rel_path(level, key), legacy_rel_path(level, key)]
    return [f"{DIGEST_ROOT}/{base}"] + [f"{SUBDIR[lv]}/{base}" for lv in LEVELS]


def migration_plan(vault: str) -> List[Tuple[str, str]]:
    """旧扁平布局 -> 新层级布局的搬迁计划 [(src_rel, dst_rel)]，幂等。

    只处理 digest/ 根下的总结卡；新目录里已就位的、以及无法识别的文件都不动。
    目标已存在同名文件时跳过（交由人工确认，避免覆盖）。
    """
    plan: List[Tuple[str, str]] = []
    flat = os.path.join(vault, DIGEST_ROOT)
    if not os.path.isdir(flat):
        return plan
    for fn in sorted(os.listdir(flat)):
        src_full = os.path.join(flat, fn)
        if not os.path.isfile(src_full) or not fn.endswith(".md"):
            continue
        hit = classify_filename(fn)
        if hit is None:
            continue
        level, key = hit
        src_rel = f"{DIGEST_ROOT}/{fn}"
        dst_rel = rel_path(level, key)
        if src_rel == dst_rel:
            continue
        dst_full = os.path.join(vault, *dst_rel.split("/"))
        if os.path.exists(dst_full):
            continue
        plan.append((src_rel, dst_rel))
    return plan
