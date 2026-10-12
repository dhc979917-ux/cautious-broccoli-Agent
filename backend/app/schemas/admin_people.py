from typing import Any, Literal

from pydantic import BaseModel, Field


class DepartmentCreate(BaseModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    parent_id: int | None = Field(default=None, gt=0)


class DepartmentUpdate(BaseModel):
    revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: int | None = Field(default=None, gt=0)
    is_active: bool | None = None


class RoleTemplateCreate(BaseModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    permissions: list[str] = Field(min_length=1, max_length=100)


class RoleTemplateUpdate(BaseModel):
    revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    permissions: list[str] | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


class PersonUpdate(BaseModel):
    revision: int = Field(ge=0)
    department_id: int | None = Field(default=None, gt=0)
    job_title: str | None = Field(default=None, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    source: str | None = Field(default=None, max_length=80)
    employment_status: Literal["active", "departed"] | None = None
    is_active: bool | None = None
    role: Literal["admin", "operator", "viewer"] | None = None


class ResourceGrantsUpdate(BaseModel):
    revision: int = Field(ge=0)
    agent_ids: list[int] | None = None
    skill_ids: list[int] | None = None
    package_ids: list[int] | None = None


class RoleGrant(BaseModel):
    revision: int = Field(ge=0)
    template_id: int = Field(gt=0)
    scope_type: Literal["global", "department", "departments", "self", "resource"]
    scope: Any = None


class RoleRevoke(BaseModel):
    revision: int = Field(ge=0)
