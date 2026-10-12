"""Add gateway directory metadata and bounded model test history."""

import argparse

from sqlalchemy import inspect

from app.database import engine
from app.models.model_governance import GatewayModelEntry, ModelMetadata, ModelTestRun


TABLES = (GatewayModelEntry.__table__, ModelMetadata.__table__, ModelTestRun.__table__)


def migrate(apply: bool = False, database_engine=None) -> list[str]:
    target = database_engine if database_engine is not None else engine
    existing = set(inspect(target).get_table_names())
    if "users" not in existing or "model_access_policies" not in existing:
        raise RuntimeError("Stage-0 identity and model policy tables are required")
    pending = [table.name for table in TABLES if table.name not in existing]
    if apply:
        for table in TABLES:
            table.create(bind=target, checkfirst=True)
    print("Stage-2 schema: " + (", ".join(pending) if pending else "up to date"))
    return pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(parser.parse_args().apply)
