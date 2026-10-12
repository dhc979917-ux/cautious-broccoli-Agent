"""Validated public Skill Hub requests; no platform-admin ownership bypass."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class TextModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

class Dependency(BaseModel):
    kind: Literal['skill', 'tool', 'model']
    skill_id: int | None = None
    version_id: str | None = None
    server_id: int | None = None
    tool_name: str | None = None
    schema_digest: str | None = None
    model_id: str | None = None



class Create(TextModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default='', max_length=2000)
    content: str = Field(min_length=1, max_length=1000000)


class Fork(TextModel):
    title: str | None = Field(default=None, max_length=200)
    version_id: str | None = None


class Edit(BaseModel):
    revision: int = Field(ge=0)
    operation: Literal['write', 'delete', 'rename'] = 'write'
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(default='', max_length=1000000)
    new_path: str | None = Field(default=None, max_length=500)


class Details(TextModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default='', max_length=2000)
    category: str = Field(default='其他', max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default='', max_length=20000)
    example: str = Field(default='', max_length=4000)
    requirements: str = Field(default='', max_length=4000)
    risks: str = Field(default='', max_length=4000)
    license: str = Field(default='', max_length=4000)
    icon: str = Field(default='', max_length=100)
    dependencies: dict[str, list[Dependency]] = Field(default_factory=dict)


class Metadata(BaseModel):
    revision: int = Field(ge=0)
    details: Details


class Release(BaseModel):
    revision: int = Field(ge=0)
    version_id: str | None = None


class Test(BaseModel):
    scripts: dict[str, str] = Field(default_factory=dict)
    script: str | None = Field(default=None, max_length=240)
    sample_input: str = Field(default='', max_length=4000)


class Share(BaseModel):
    shared_to: int = Field(gt=0)
    allow_fork: bool = False
    expires_at: datetime | None = None


class Decision(BaseModel):
    approved: bool
    note: str = Field(default='', max_length=4000)


class Note(TextModel):
    note: str = Field(min_length=1, max_length=4000)


class Block(Note):
    version_id: str | None = None
    descendants: bool = False


class Review(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default='', max_length=4000)


class Flag(BaseModel):
    value: bool
