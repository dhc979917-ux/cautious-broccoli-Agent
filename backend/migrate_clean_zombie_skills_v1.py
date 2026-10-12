"""Clean up zombie skill records whose directories no longer exist on disk.

Scans the skills and skill_packages tables for rows flagged dir_missing=True,
optionally cross-checks the filesystem, then deletes the confirmed zombies.
Safe to re-run (idempotent): only touches rows already marked dir_missing.

Usage (inside backend container):
    python migrate_clean_zombie_skills_v1.py [--dry-run] [--skip-fs-check]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Root of all skill directories inside the container.
SKILLS_ROOT = "/app/app/agents"


def _resolve_abs_path(rel_path: str) -> str | None:
    """Convert a rel_path (relative to skills root) to an absolute path."""
    if not rel_path:
        return None
    return os.path.join(SKILLS_ROOT, rel_path)


def clean_skills(dry_run: bool, skip_fs_check: bool) -> int:
    """Delete skill records where dir_missing=True."""
    from app.database import SessionLocal
    from app.models.skill import Skill

    db = SessionLocal()
    deleted = 0
    try:
        zombies = db.query(Skill).filter(Skill.dir_missing.is_(True)).all()
        if not zombies:
            print("[migrate_clean_zombie_skills_v1] No zombie skill records found.")
            return 0

        for skill in zombies:
            abs_path = _resolve_abs_path(skill.rel_path or "")
            if not skip_fs_check and abs_path and os.path.isdir(abs_path):
                print(f"[WARN] Skill '{skill.name}' (id={skill.id}) has dir_missing=True "
                      f"but directory exists at {abs_path} - skipping.")
                continue
            print(f"[INFO] Deleting zombie skill '{skill.name}' (id={skill.id}, "
                  f"rel_path={skill.rel_path}, is_active={skill.is_active})")
            if not dry_run:
                db.delete(skill)
            deleted += 1

        if not dry_run and deleted > 0:
            db.commit()
        action = "Would delete" if dry_run else "Deleted"
        print(f"[OK] {action} {deleted} zombie skill record(s).")
        return deleted
    finally:
        db.close()


def clean_packages(dry_run: bool, skip_fs_check: bool) -> int:
    """Delete skill_package records where dir_missing=True."""
    from app.database import SessionLocal
    from app.models.skill_package import SkillPackage

    db = SessionLocal()
    deleted = 0
    try:
        zombies = db.query(SkillPackage).filter(SkillPackage.dir_missing.is_(True)).all()
        if not zombies:
            print("[migrate_clean_zombie_skills_v1] No zombie package records found.")
            return 0

        for pkg in zombies:
            abs_path = _resolve_abs_path(pkg.rel_path or "")
            if not skip_fs_check and abs_path and os.path.isdir(abs_path):
                print(f"[WARN] Package '{pkg.name}' (id={pkg.id}) has dir_missing=True "
                      f"but directory exists at {abs_path} - skipping.")
                continue
            print(f"[INFO] Deleting zombie package '{pkg.name}' (id={pkg.id}, "
                  f"rel_path={pkg.rel_path})")
            if not dry_run:
                db.delete(pkg)
            deleted += 1

        if not dry_run and deleted > 0:
            db.commit()
        action = "Would delete" if dry_run else "Deleted"
        print(f"[OK] {action} {deleted} zombie package record(s).")
        return deleted
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be deleted without actually deleting")
    parser.add_argument("--skip-fs-check", action="store_true",
                        help="Skip filesystem verification (trust dir_missing flag)")
    args = parser.parse_args()

    print("[migrate_clean_zombie_skills_v1] Starting zombie cleanup...")
    total = clean_skills(args.dry_run, args.skip_fs_check)
    total += clean_packages(args.dry_run, args.skip_fs_check)
    print(f"[migrate_clean_zombie_skills_v1] Done. Total: {total} record(s) "
          f"{'would be ' if args.dry_run else ''}deleted.")


if __name__ == "__main__":
    main()
