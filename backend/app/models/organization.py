from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True)
    code = Column(String(80), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class RoleTemplate(Base):
    __tablename__ = "role_templates"

    id = Column(Integer, primary_key=True)
    code = Column(String(80), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    permissions_json = Column(Text, nullable=False, default="[]", server_default="[]")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class UserRoleAssignment(Base):
    __tablename__ = "user_role_assignments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_template_id = Column(Integer, ForeignKey("role_templates.id", ondelete="RESTRICT"), nullable=False)
    scope_type = Column(String(24), nullable=False)
    scope_json = Column(Text, nullable=False, default="[]", server_default="[]")
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "role_template_id", "scope_type", "scope_json", name="uq_user_role_scope"),
    )
