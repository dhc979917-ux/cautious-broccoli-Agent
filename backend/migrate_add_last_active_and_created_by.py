# -*- coding: utf-8 -*-
"""Migration: add last_active to users, created_by to skills and skill_packages.
Backfill created_by from skill_upload_jobs."""
import sqlite3, sys, os

def migrate(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "last_active" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN last_active TIMESTAMP")
        print("Added users.last_active")
    cols = [r[1] for r in c.execute("PRAGMA table_info(skills)").fetchall()]
    if "created_by" not in cols:
        c.execute("ALTER TABLE skills ADD COLUMN created_by INTEGER")
        print("Added skills.created_by")
    cols = [r[1] for r in c.execute("PRAGMA table_info(skill_packages)").fetchall()]
    if "created_by" not in cols:
        c.execute("ALTER TABLE skill_packages ADD COLUMN created_by INTEGER")
        print("Added skill_packages.created_by")
    # Backfill from upload jobs
    c.execute("""UPDATE skill_packages SET created_by = (
        SELECT j.created_by FROM skill_upload_jobs j
        WHERE j.package_name = skill_packages.name AND j.created_by IS NOT NULL
        ORDER BY j.created_at DESC LIMIT 1
    ) WHERE created_by IS NULL""")
    print(f"Backfilled skill_packages.created_by: {c.rowcount} rows")
    c.execute("""UPDATE skills SET created_by = (
        SELECT sp.created_by FROM skill_packages sp
        WHERE sp.id = skills.package_id AND sp.created_by IS NOT NULL
    ) WHERE skills.package_id IS NOT NULL AND skills.created_by IS NULL""")
    print(f"Backfilled skills.created_by: {c.rowcount} rows")
    conn.commit()
    conn.close()
    print("Migration done")

if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "app.db")
    migrate(db)
