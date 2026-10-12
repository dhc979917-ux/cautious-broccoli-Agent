"""一次性数据迁移：SQLite(app.db) -> PostgreSQL。

用法（backend 容器内，DATABASE_URL 已指向 PostgreSQL）：
    python migrate_sqlite_to_pg.py /tmp/app.db

步骤：
  1. init_db() 按 ORM 模型建表（含补列迁移）
  2. 读取 SQLite 全部表，按外键依赖拓扑排序后依次 INSERT
  3. Boolean / JSON / DateTime 值自动转换
  4. 自增序列回填为 MAX(id)+1（否则新数据主键冲突）
幂等：PG 侧已有数据的表整体跳过，可安全重跑。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, MetaData, create_engine, text


def _parse_dt(v):
    """SQLite 里的时间戳是字符串，转成 datetime 交给 PG。"""
    if not isinstance(v, str):
        return v
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return v


def topo_order(src):
    """按 SQLite 外键依赖做拓扑排序（父表在前），有环则兜底直出。"""
    tables = [r["name"] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    tset = set(tables)
    remaining = {}
    for t in tables:
        refs = {r["table"] for r in src.execute(f'PRAGMA foreign_key_list("{t}")')}
        remaining[t] = (refs & tset) - {t}
    ordered, placed = [], set()
    while remaining:
        progress = False
        for t in list(remaining):
            if remaining[t] <= placed:
                ordered.append(t)
                placed.add(t)
                del remaining[t]
                progress = True
        if not progress:
            ordered.extend(remaining)
            break
    return ordered


def main():
    if len(sys.argv) < 2:
        print("用法: python migrate_sqlite_to_pg.py /path/to/app.db")
        sys.exit(2)
    sqlite_path = sys.argv[1]

    url = os.environ.get("DATABASE_URL", "")
    if "postgresql" not in url:
        print("DATABASE_URL 未指向 PostgreSQL，拒绝执行")
        sys.exit(1)

    # 1) 建表（模型为准，复用应用自带的 init_db 补列逻辑）
    from app.database import init_db
    init_db()
    print("[ok] PG 建表完成")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    engine = create_engine(url)
    meta = MetaData()
    meta.reflect(bind=engine)

    total = 0
    for tname in topo_order(src):
        table = meta.tables.get(tname)
        if table is None:
            print(f"[skip] {tname}: PG 无对应表")
            continue
        with engine.begin() as conn:
            if conn.execute(table.select().limit(1)).first() is not None:
                print(f"[skip] {tname}: PG 已有数据")
                continue
        cols = {c.name: c for c in table.columns}
        rows = src.execute(f'SELECT * FROM "{tname}"').fetchall()
        if not rows:
            print(f"[ok] {tname}: 空表")
            continue
        src_cols = [k for k in rows[0].keys() if k in cols]
        data = []
        for r in rows:
            item = {}
            for c in src_cols:
                v = r[c]
                if v is not None:
                    ctype = cols[c].type
                    if isinstance(ctype, Boolean):
                        v = bool(v)
                    elif isinstance(ctype, JSON) and isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except json.JSONDecodeError:
                            pass
                    elif isinstance(ctype, DateTime):
                        v = _parse_dt(v)
                item[c] = v
            data.append(item)
        with engine.begin() as conn:
            conn.execute(table.insert(), data)
        total += len(data)
        print(f"[ok] {tname}: {len(data)} 行")

    # 4) 序列回填
    with engine.begin() as conn:
        for tname, table in meta.tables.items():
            pk = list(table.primary_key.columns)
            if len(pk) == 1 and isinstance(pk[0].type, Integer):
                seq = conn.execute(
                    text(f"SELECT pg_get_serial_sequence('{tname}', '{pk[0].name}')")
                ).scalar()
                if seq:
                    conn.execute(text(
                        f'SELECT setval(\'{seq}\', COALESCE(MAX("{pk[0].name}"), 0) + 1, false) FROM "{tname}"'
                    ))
    print("[ok] 自增序列已回填")

    src.close()
    print(f"迁移完成：共 {total} 行")


if __name__ == "__main__":
    main()
