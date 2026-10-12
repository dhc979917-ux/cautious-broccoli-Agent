"""Add organization metadata and scoped role storage without rewriting users.

Check without arguments; apply only after a verified backup of the current 52 database.
"""

import argparse

from sqlalchemy import inspect, text

from app.database import engine
from app.models.organization import Department, RoleTemplate, UserRoleAssignment
from app.models.user import User


USER_COLUMNS = {
    "department_id": "INTEGER REFERENCES departments(id) ON DELETE SET NULL",
    "job_title": "VARCHAR(120)",
    "external_id": "VARCHAR(160)",
    "source": "VARCHAR(80)",
    "employment_status": "VARCHAR(20)",
    "revision": "INTEGER NOT NULL DEFAULT 0",
}
NEW_TABLES = (Department.__table__, RoleTemplate.__table__, UserRoleAssignment.__table__)
EXTERNAL_INDEX = next(index for index in User.__table__.indexes if index.name == "uq_users_source_external")


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    inspector = inspect(target)
    if "users" not in inspector.get_table_names():
        raise RuntimeError("Existing users table is required")
    existing_tables = set(inspector.get_table_names())
    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("users")}
    missing_tables = [table for table in NEW_TABLES if table.name not in existing_tables]
    missing_columns = [name for name in USER_COLUMNS if name not in existing_columns]
    missing_index = EXTERNAL_INDEX.name not in existing_indexes
    pending = [table.name for table in missing_tables]
    pending.extend(f"users.{name}" for name in missing_columns)
    if missing_index:
        pending.append(EXTERNAL_INDEX.name)
    if apply:
        for table in NEW_TABLES:
            table.create(bind=target, checkfirst=True)
        with target.begin() as connection:
            for name in missing_columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {USER_COLUMNS[name]}"))
        EXTERNAL_INDEX.create(bind=target, checkfirst=True)
    print("Stage-1 schema: " + (", ".join(pending) if pending else "up to date"))
    return pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(parser.parse_args().apply)
