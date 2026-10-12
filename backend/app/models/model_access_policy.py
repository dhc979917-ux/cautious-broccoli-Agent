"""Chat-model allowlists keyed by role or individual user."""

from sqlalchemy import Boolean, Column, JSON, String

from app.database import Base


class ModelAccessPolicy(Base):
    __tablename__ = "model_access_policies"

    scope = Column(String(10), primary_key=True)
    principal = Column(String(64), primary_key=True)
    allowed_models = Column(JSON, nullable=False)


class ModelRoleOverride(Base):
    __tablename__ = "model_role_overrides"

    role = Column(String(10), primary_key=True)
    model_id = Column(String(200), primary_key=True)
    allowed = Column(Boolean, nullable=False)


class ModelPolicyMigration(Base):
    __tablename__ = "model_policy_migrations"

    name = Column(String(100), primary_key=True)
