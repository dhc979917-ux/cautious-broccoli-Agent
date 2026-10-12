"""一次性迁移脚本：技能磁盘身份 v7（rel_path / dir_missing）+ 上传作业表（可安全重复执行）。

变更内容：
1. skills / skill_packages 新增 rel_path（相对 app/agents/ 的目录路径）与 dir_missing
2. 新建 skill_upload_jobs 表（技能包上传异步作业的状态载体）
3. 回填存量行的 rel_path（按目录名在磁盘上匹配），目录已不在的行标记 dir_missing
4. 为 rel_path 建唯一索引（存在重复路径时跳过并告警，不硬建）

说明：服务启动时 init_db() 也会补齐这些列（幂等），本脚本是仓库约定的独立迁移入口，
额外负责回填、建索引与校验。同步逻辑本身也会给 rel_path 为空的行补路径，
因此脚本不是正确性前置条件，但部署后跑一次能立刻拿到一份对账报告。

用法（在 backend 目录下）：
    python migrate_skills_v7.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TABLES = ("skills", "skill_packages")
NEW_COLUMNS = ("rel_path", "dir_missing")


def ensure_schema():
    """建表 + 补列：直接复用启动时的 init_db（create_all + _migrate_add_columns，幂等）。"""
    from app.database import init_db
    from app.services.skill_upload_service import ensure_tables
    init_db()
    ensure_tables()
    print("[OK] 表结构与新增列已就绪（init_db 幂等）")


def backfill_rel_path():
    """按目录名把磁盘路径回填到存量行；磁盘上找不到的行标记 dir_missing。

    只补路径与缺失标记，不动 is_active——停用/恢复的判定权交给同步逻辑，
    避免迁移脚本与同步各说一套。
    """
    from app.database import SessionLocal
    from app.models.skill import Skill
    from app.models.skill_package import SkillPackage
    from app.services.skill_service import SkillService, index_skills_by_name, scan_skill_dirs

    db = SessionLocal()
    try:
        entries = scan_skill_dirs(SkillService._skills_roots())
        by_name, shadowed = index_skills_by_name(entries)
        for line in shadowed:
            print(f"[WARN] 同名技能目录: {line}")
        svc = SkillService(db)
        skill_filled = skill_missing = 0
        for row in db.query(Skill).all():
            if row.rel_path:
                continue
            entry = by_name.get(row.name)
            if entry:
                row.rel_path = entry.rel_path
                row.dir_missing = False
                skill_filled += 1
            else:
                row.dir_missing = True
                skill_missing += 1
                print(f"[WARN] 技能 '{row.name}' 磁盘上找不到目录 → 标记 dir_missing"
                      "（管理面板可恢复或彻底删除记录）")
        pkg_filled = pkg_missing = 0
        for pkg in db.query(SkillPackage).all():
            if pkg.rel_path:
                continue
            resolved, _candidates = svc._resolve_package_dir(None, pkg.name)
            if resolved:
                pkg.rel_path = SkillService._rel_from_abs(resolved)
                pkg.dir_missing = False
                pkg_filled += 1
            else:
                pkg.dir_missing = True
                pkg_missing += 1
                print(f"[WARN] 技能包 '{pkg.name}' 磁盘上找不到目录 → 标记 dir_missing")
        db.commit()
        print(f"[OK] rel_path 回填：技能 {skill_filled} 条 / 技能包 {pkg_filled} 条；"
              f"目录缺失：技能 {skill_missing} 条 / 技能包 {pkg_missing} 条")
    finally:
        db.close()


def ensure_indexes():
    """rel_path 唯一索引：同一目录不允许对应多条记录（有重复时如实告警，不硬建）。"""
    from sqlalchemy import inspect, text
    from app.database import engine
    insp = inspect(engine)
    for table in TABLES:
        if not insp.has_table(table):
            continue
        index_name = f"ix_{table}_rel_path"
        if index_name in {ix["name"] for ix in insp.get_indexes(table)}:
            print(f"[INFO] {index_name} 已存在，跳过")
            continue
        with engine.connect() as conn:
            dup = conn.execute(text(
                f"SELECT rel_path, COUNT(*) AS n FROM {table} "
                "WHERE rel_path IS NOT NULL GROUP BY rel_path HAVING COUNT(*) > 1"
            )).fetchall()
            if dup:
                print(f"[WARN] {table} 存在重复 rel_path，跳过唯一索引，请人工清理: {dup[:5]}")
                continue
            conn.execute(text(f"CREATE UNIQUE INDEX {index_name} ON {table}(rel_path)"))
            conn.commit()
        print(f"[OK] 已创建唯一索引 {index_name}")


def verify():
    from sqlalchemy import inspect
    from app.database import engine
    insp = inspect(engine)
    for table in TABLES:
        cols = [c["name"] for c in insp.get_columns(table)]
        for col in NEW_COLUMNS:
            if col not in cols:
                raise SystemExit(f"[migrate_skills_v7] 缺列: {table}.{col}")
    if not insp.has_table("skill_upload_jobs"):
        raise SystemExit("[migrate_skills_v7] 缺表: skill_upload_jobs")
    print("[OK] migrate_skills_v7 校验通过：新列齐备、skill_upload_jobs 已就绪")


def main():
    ensure_schema()
    backfill_rel_path()
    ensure_indexes()
    verify()


if __name__ == "__main__":
    main()
