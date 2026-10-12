"""Add independent cache pricing and migrate model permissions once."""

import argparse
import json

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.database import engine
from app.models.model_access_policy import ModelAccessPolicy, ModelPolicyMigration, ModelRoleOverride
from app.models.model_governance import GatewayModelEntry, ModelMetadata
from app.models.user import User
from app.services.model_permissions import default_allowed


MIGRATION = "model_gateway_v2"


def migrate(apply=False, database_engine=None):
    target = database_engine if database_engine is not None else engine
    inspector = inspect(target)
    tables = set(inspector.get_table_names())
    if not {"users", "model_access_policies", "model_metadata", "gateway_model_entries"} <= tables:
        raise RuntimeError("Identity and model-governance migrations must run first")
    pending_column = "cached_input_price_per_million" not in {
        column["name"] for column in inspector.get_columns("model_metadata")}
    completed = False
    if "model_policy_migrations" in tables:
        with target.connect() as connection:
            completed = connection.execute(text(
                "SELECT name FROM model_policy_migrations WHERE name = :name"),
                {"name": MIGRATION}).first() is not None
    changes = []
    with Session(target) as db:
        if not completed:
            catalog = [row.model_id for row in db.query(GatewayModelEntry)]
            for policy in db.query(ModelAccessPolicy):
                if policy.scope == "role":
                    changes.append({"scope": "role", "principal": policy.principal,
                                    "action": "reset_to_prefix_defaults"})
                else:
                    user = db.get(User, int(policy.principal)) if policy.principal.isdecimal() else None
                    extras = sorted({model_id for model_id in policy.allowed_models or []
                                     if model_id in catalog and user and
                                     not default_allowed(user.role, model_id)})
                    changes.append({"scope": "user", "principal": policy.principal,
                                    "extra_models": extras})
    print(json.dumps({"add_cache_price": pending_column, "completed": completed,
                      "policy_changes": changes}, ensure_ascii=False))
    if not apply:
        return changes
    with target.begin() as connection:
        if target.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(5201010)"))
        if "cached_input_price_per_million" not in {
                column["name"] for column in inspect(connection).get_columns("model_metadata")}:
            connection.execute(text(
                "ALTER TABLE model_metadata ADD COLUMN cached_input_price_per_million NUMERIC(14,6)"))
        ModelRoleOverride.__table__.create(connection, checkfirst=True)
        ModelPolicyMigration.__table__.create(connection, checkfirst=True)
        with Session(bind=connection) as db:
            if not db.get(ModelPolicyMigration, MIGRATION):
                changes_by_key = {(item["scope"], item["principal"]): item for item in changes}
                for policy in db.query(ModelAccessPolicy):
                    change = changes_by_key[(policy.scope, policy.principal)]
                    if policy.scope == "role" or not change.get("extra_models"):
                        db.delete(policy)
                    else:
                        policy.allowed_models = change["extra_models"]
                db.add(ModelPolicyMigration(name=MIGRATION))
                db.flush()
    return changes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    migrate(apply=parser.parse_args().apply)
