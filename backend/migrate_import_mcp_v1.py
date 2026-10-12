"""Import MCP server configs from a JSON seed file into the mcp_servers table.

Reads mcp_servers_seed.json (sibling file), upserts each entry by name.
Existing records are updated; new ones are inserted.  Idempotent and safe
to re-run.  Extensible: just add more entries to the JSON file.

Usage (inside backend container):
    python migrate_import_mcp_v1.py [--seed /path/to/seed.json] [--dry-run]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mcp_servers_seed.json")


def load_seed(seed_path: str) -> list[dict]:
    with open(seed_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Seed file must contain a JSON array, got {type(data).__name__}")
    return data


def upsert_mcp_server(db, model, entry: dict) -> str:
    """Insert or update a single MCP server record by name.
    Returns 'inserted' or 'updated'."""
    name = entry["name"]
    existing = db.query(model).filter(model.name == name).first()
    fields = {"type", "command", "args_json", "env_json", "url",
              "description", "enabled"}
    if existing:
        for field in fields:
            if field in entry:
                setattr(existing, field, entry[field])
        return "updated"
    else:
        row = model(name=name)
        for field in fields:
            if field in entry:
                setattr(row, field, entry[field])
        db.add(row)
        return "inserted"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=DEFAULT_SEED,
                        help=f"Path to seed JSON file (default: {DEFAULT_SEED})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing to DB")
    args = parser.parse_args()

    entries = load_seed(args.seed)
    print(f"[migrate_import_mcp_v1] Loaded {len(entries)} MCP server(s) from {args.seed}")

    from app.database import SessionLocal
    from app.models.mcp_server import McpServer

    db = SessionLocal()
    inserted = updated = 0
    try:
        for entry in entries:
            action = upsert_mcp_server(db, McpServer, entry)
            if action == "inserted":
                inserted += 1
                print(f"[INFO] Inserted MCP server '{entry['name']}'")
            else:
                updated += 1
                print(f"[INFO] Updated MCP server '{entry['name']}'")
        if not args.dry_run:
            db.commit()
            print(f"[OK] Committed: {inserted} inserted, {updated} updated.")
        else:
            db.rollback()
            print(f"[OK] Dry run: would insert {inserted}, update {updated}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
