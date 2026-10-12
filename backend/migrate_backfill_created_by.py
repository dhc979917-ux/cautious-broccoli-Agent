"""Backfill created_by on skills and skill_packages from upload job history.

Run inside the backend container:
    python migrate_backfill_created_by.py

Safe to re-run: only touches rows where created_by IS NULL.
"""
from app.database import engine
from sqlalchemy import text


def migrate():
    """Backfill created_by in three passes: packages -> member skills -> standalone skills."""
    with engine.begin() as conn:
        # 1. Packages: match by name to the most recent done upload job
        result = conn.execute(text("""
            UPDATE skill_packages SET created_by = (
                SELECT j.created_by FROM skill_upload_jobs j
                WHERE j.package_name = skill_packages.name
                  AND j.status = 'done' AND j.created_by IS NOT NULL
                ORDER BY j.id DESC LIMIT 1
            )
            WHERE skill_packages.created_by IS NULL
              AND EXISTS (
                SELECT 1 FROM skill_upload_jobs j
                WHERE j.package_name = skill_packages.name
                  AND j.status = 'done' AND j.created_by IS NOT NULL
              )
        """))
        print(f"[1/3] Packages backfilled: {result.rowcount} rows")

        # 2. Skills inside packages: inherit from their package's created_by
        result = conn.execute(text("""
            UPDATE skills SET created_by = sp.created_by
            FROM skill_packages sp
            WHERE skills.package_id = sp.id
              AND skills.created_by IS NULL
              AND sp.created_by IS NOT NULL
        """))
        print(f"[2/3] Member skills backfilled: {result.rowcount} rows")

        # 3. Standalone skills (no package_id): match by name to upload jobs
        result = conn.execute(text("""
            UPDATE skills SET created_by = (
                SELECT j.created_by FROM skill_upload_jobs j
                WHERE j.package_name = skills.name
                  AND j.status = 'done' AND j.created_by IS NOT NULL
                ORDER BY j.id DESC LIMIT 1
            )
            WHERE skills.created_by IS NULL
              AND skills.package_id IS NULL
              AND EXISTS (
                SELECT 1 FROM skill_upload_jobs j
                WHERE j.package_name = skills.name
                  AND j.status = 'done' AND j.created_by IS NOT NULL
              )
        """))
        print(f"[3/3] Standalone skills backfilled: {result.rowcount} rows")

    print("Done. created_by backfill complete.")


if __name__ == "__main__":
    migrate()
