"""Bounded model proposals; model arguments never contain an actor identity."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['write', 'patch', 'delete']
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(default='', max_length=100000)
    old_string: str = Field(default='', max_length=100000)
    new_string: str = Field(default='', max_length=100000)


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: int
    quote: str = Field(min_length=8, max_length=1000)
    kind: Literal['user_correction', 'observed_result']


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['no_change', 'repair_personal', 'derive_source', 'create_personal']
    reason: str = Field(max_length=2000)
    resource_id: str | None = None
    title: str = Field(default='', max_length=200)
    summary: str = Field(default='', max_length=2000)
    operations: list[Operation] = Field(default_factory=list, max_length=16)
    evidence: list[Evidence] = Field(default_factory=list, max_length=5)
    sample_input: str = Field(default='', max_length=4000)
    criteria: list[str] = Field(default_factory=list, max_length=8)
    script: str | None = Field(default=None, max_length=240)


class Preference(BaseModel):
    enabled: bool


class Pause(BaseModel):
    paused: bool


class Confirmation(BaseModel):
    acknowledge_risk: bool = False


class Target(BaseModel):
    resource_id: str
