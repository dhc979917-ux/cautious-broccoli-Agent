"""Local unit test for migrate_clean_zombie_skills_v1 and migrate_import_mcp_v1.

Uses an in-memory SQLite database to simulate the production schema.
Run:  python test/_test_migrate_scripts.py
"""
import json
import os
import sys
import tempfile

# Make backend importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.mcp_server import McpServer


def setup_db():
    """Create an in-memory SQLite DB with the production schema."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def seed_skills(db):
    """Insert test data: 3 normal skills + 2 zombie skills."""
    normal = [
        Skill(name="fer-analysis", display_name="FER Analysis",
              rel_path="skills/semiconductor-skills-export/fer-analysis",
              dir_missing=False, is_active=True, package_id=7),
        Skill(name="fdc-data-export", display_name="FDC Data Export",
              rel_path="skills/semiconductor-skills-export/04-fdc-data-export/fdc-data-export",
              dir_missing=False, is_active=True, package_id=7),
        Skill(name="ocr", display_name="OCR",
              rel_path="skills/ocr",
              dir_missing=False, is_active=True),
    ]
    zombies = [
        Skill(name="wipagent-ai-skills", display_name="WipAgent AI Skills",
              rel_path="skills/semiconductor-skills-export/01-wipagent-system/wipagent-ai-skills",
              dir_missing=True, is_active=False, package_id=7),
        Skill(name="wip-lot-tracking", display_name="WIP Lot Tracking",
              rel_path="skills/semiconductor-skills-export/02-wip-conversation-skills/wip-lot-tracking",
              dir_missing=True, is_active=False, package_id=7),
    ]
    for s in normal + zombies:
        db.add(s)
    db.commit()
    return len(normal), len(zombies)


def seed_mcp(db):
    """Insert one existing MCP server (to test upsert)."""
    db.add(McpServer(name="web", type="stdio", command="python3",
                     args_json='["/app/app/agents/mcp_servers/web_mcp_server.py"]',
                     env_json="{}", description="existing web tool",
                     enabled=True))
    db.commit()


def test_clean_zombie_skills():
    """Verify that zombie skill records are deleted and normal ones survive."""
    engine, db = setup_db()
    n_normal, n_zombie = seed_skills(db)
    # also add a zombie package
    db.add(SkillPackage(name="old-pkg", display_name="Old Package",
                        rel_path="skills/old-pkg", dir_missing=True))
    db.commit()

    total_before = db.query(Skill).count()
    zombie_before = db.query(Skill).filter(Skill.dir_missing.is_(True)).count()
    pkg_zombie_before = db.query(SkillPackage).filter(SkillPackage.dir_missing.is_(True)).count()
    assert zombie_before == n_zombie, f"Expected {n_zombie} zombies, got {zombie_before}"
    assert pkg_zombie_before == 1, f"Expected 1 zombie pkg, got {pkg_zombie_before}"

    # Run the cleanup with --skip-fs-check (SQLite has no real filesystem paths)
    # We call the functions directly by monkeypatching the SessionLocal
    import app.database as dbmod
    orig_session = dbmod.SessionLocal
    dbmod.SessionLocal = sessionmaker(bind=engine)
    try:
        # Import after patching
        import importlib
        import migrate_clean_zombie_skills_v1 as m
        importlib.reload(m)
        m.clean_skills(dry_run=False, skip_fs_check=True)
        m.clean_packages(dry_run=False, skip_fs_check=True)
    finally:
        dbmod.SessionLocal = orig_session

    remaining = db.query(Skill).count()
    zombie_after = db.query(Skill).filter(Skill.dir_missing.is_(True)).count()
    pkg_zombie_after = db.query(SkillPackage).filter(SkillPackage.dir_missing.is_(True)).count()
    normal_after = db.query(Skill).filter(Skill.dir_missing.is_(False)).count()

    assert remaining == n_normal, f"Expected {n_normal} remaining, got {remaining}"
    assert zombie_after == 0, f"Expected 0 zombies after cleanup, got {zombie_after}"
    assert pkg_zombie_after == 0, f"Expected 0 zombie pkgs, got {pkg_zombie_after}"
    assert normal_after == n_normal, f"Normal skills should survive, got {normal_after}"
    db.close()
    print("[PASS] test_clean_zombie_skills")


def test_import_mcp_upsert():
    """Verify that MCP servers are inserted/updated correctly."""
    engine, db = setup_db()
    seed_mcp(db)

    # Load seed file
    seed_path = os.path.join(os.path.dirname(__file__), "..", "backend",
                             "mcp_servers_seed.json")
    with open(seed_path, encoding="utf-8") as f:
        entries = json.load(f)
    assert len(entries) == 3, f"Expected 3 seed entries, got {len(entries)}"

    # Patch SessionLocal
    import app.database as dbmod
    orig_session = dbmod.SessionLocal
    dbmod.SessionLocal = sessionmaker(bind=engine)
    try:
        import importlib
        import migrate_import_mcp_v1 as m
        importlib.reload(m)
        for entry in entries:
            m.upsert_mcp_server(db, McpServer, entry)
        db.commit()
    finally:
        dbmod.SessionLocal = orig_session

    total = db.query(McpServer).count()
    assert total == 4, f"Expected 4 MCP servers (1 existing + 3 new), got {total}"

    for entry in entries:
        row = db.query(McpServer).filter(McpServer.name == entry["name"]).first()
        assert row is not None, f"MCP server '{entry['name']}' not found"
        assert row.type == entry["type"], f"Wrong type for {entry['name']}"
        assert row.url == entry["url"], f"Wrong URL for {entry['name']}"
        assert row.enabled == entry["enabled"], f"Wrong enabled for {entry['name']}"

    # Verify the existing "web" server was NOT overwritten by the seed
    web = db.query(McpServer).filter(McpServer.name == "web").first()
    assert web is not None, "Existing web server should still be there"
    assert "existing web tool" in web.description, "Existing server should not be overwritten"

    db.close()
    print("[PASS] test_import_mcp_upsert")


def test_import_mcp_idempotent():
    """Running the import twice should not create duplicates."""
    engine, db = setup_db()

    seed_path = os.path.join(os.path.dirname(__file__), "..", "backend",
                             "mcp_servers_seed.json")
    with open(seed_path, encoding="utf-8") as f:
        entries = json.load(f)

    import app.database as dbmod
    orig_session = dbmod.SessionLocal
    dbmod.SessionLocal = sessionmaker(bind=engine)
    try:
        import importlib
        import migrate_import_mcp_v1 as m
        importlib.reload(m)
        # First run
        for entry in entries:
            m.upsert_mcp_server(db, McpServer, entry)
        db.commit()
        count1 = db.query(McpServer).count()
        # Second run (should update, not insert)
        for entry in entries:
            m.upsert_mcp_server(db, McpServer, entry)
        db.commit()
        count2 = db.query(McpServer).count()
    finally:
        dbmod.SessionLocal = orig_session

    assert count1 == 3, f"Expected 3 after first run, got {count1}"
    assert count2 == 3, f"Expected 3 after second run, got {count2}"
    db.close()
    print("[PASS] test_import_mcp_idempotent")


def test_clean_zombie_idempotent():
    """Running cleanup twice should not error."""
    engine, db = setup_db()
    db.add(Skill(name="zombie1", display_name="Zombie1",
                 rel_path="skills/zombie1", dir_missing=True, is_active=False))
    db.commit()

    import app.database as dbmod
    orig_session = dbmod.SessionLocal
    dbmod.SessionLocal = sessionmaker(bind=engine)
    try:
        import importlib
        import migrate_clean_zombie_skills_v1 as m
        importlib.reload(m)
        m.clean_skills(dry_run=False, skip_fs_check=True)
        m.clean_skills(dry_run=False, skip_fs_check=True)  # second run
    finally:
        dbmod.SessionLocal = orig_session

    count = db.query(Skill).count()
    assert count == 0, f"Expected 0 after cleanup, got {count}"
    db.close()
    print("[PASS] test_clean_zombie_idempotent")


if __name__ == "__main__":
    test_clean_zombie_skills()
    test_import_mcp_upsert()
    test_import_mcp_idempotent()
    test_clean_zombie_idempotent()
    print("\n=== ALL TESTS PASSED ===")
