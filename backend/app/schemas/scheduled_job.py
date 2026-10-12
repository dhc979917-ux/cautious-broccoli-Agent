"""调度任务模块请求/响应校验（Pydantic v2）"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

TRIGGER_TYPES = ("cron", "once")
MISFIRE_POLICIES = ("run_once", "skip")
NOTIFY_ONS = ("none", "failure", "always")
NOTIFY_CHANNELS = ("inapp", "email")
# 描述是表格里的一行摘要，产品侧限定 20 字以内（DB 列仍为 String(500)，
# 不缩列：历史长描述照旧完整展示，只是新建/编辑时不允许再超长）
DESCRIPTION_MAX_LEN = 20


class ScheduledJobCreate(BaseModel):
    # 公共任务必填；个人实例（传 parent_id）从父任务复制，无需提供
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=DESCRIPTION_MAX_LEN)
    prompt: Optional[str] = Field(None, max_length=5000)
    # 选择公共任务生成个人实例时传公共任务 id；管理员新建公共任务不传
    parent_id: Optional[int] = None
    # 管理员新建时可选：True=公共任务（全员可见可选）；False=个人任务（仅自己）
    # 传 parent_id 建实例时后端强制 False，此字段被忽略
    is_public: bool = True
    trigger_type: str = "cron"
    cron_expr: Optional[str] = Field(None, max_length=64)
    run_at: Optional[datetime] = None
    timezone: str = Field("Asia/Shanghai", max_length=64)
    model: Optional[str] = Field(None, max_length=100)
    selected_skills: Optional[List[str]] = None
    selected_mcps: Optional[List[int]] = None
    enabled: bool = True
    skip_if_running: bool = True
    timeout_minutes: int = Field(60, ge=0, le=1440)
    max_retries: int = Field(0, ge=0, le=3)
    retry_interval_minutes: int = Field(5, ge=1, le=1440)
    misfire_policy: str = "run_once"
    notify_on: str = "none"
    notify_channels: List[str] = Field(default_factory=list)

    @field_validator("trigger_type")
    @classmethod
    def _check_trigger(cls, v):
        if v not in TRIGGER_TYPES:
            raise ValueError(f"trigger_type 必须是 {'/'.join(TRIGGER_TYPES)}")
        return v

    @field_validator("misfire_policy")
    @classmethod
    def _check_misfire(cls, v):
        if v not in MISFIRE_POLICIES:
            raise ValueError(f"misfire_policy 必须是 {'/'.join(MISFIRE_POLICIES)}")
        return v

    @field_validator("notify_on")
    @classmethod
    def _check_notify_on(cls, v):
        if v not in NOTIFY_ONS:
            raise ValueError(f"notify_on 必须是 {'/'.join(NOTIFY_ONS)}")
        return v

    @field_validator("notify_channels")
    @classmethod
    def _check_channels(cls, v):
        bad = [c for c in v if c not in NOTIFY_CHANNELS]
        if bad:
            raise ValueError(f"notify_channels 仅支持 {'/'.join(NOTIFY_CHANNELS)}")
        return list(dict.fromkeys(v))


class ScheduledJobUpdate(BaseModel):
    """全部可选；传哪个改哪个"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=DESCRIPTION_MAX_LEN)
    prompt: Optional[str] = Field(None, min_length=1, max_length=5000)
    trigger_type: Optional[str] = None
    cron_expr: Optional[str] = Field(None, max_length=64)
    run_at: Optional[datetime] = None
    timezone: Optional[str] = Field(None, max_length=64)
    model: Optional[str] = Field(None, max_length=100)
    selected_skills: Optional[List[str]] = None
    selected_mcps: Optional[List[int]] = None
    enabled: Optional[bool] = None
    skip_if_running: Optional[bool] = None
    timeout_minutes: Optional[int] = Field(None, ge=0, le=1440)
    max_retries: Optional[int] = Field(None, ge=0, le=3)
    retry_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    misfire_policy: Optional[str] = None
    notify_on: Optional[str] = None
    notify_channels: Optional[List[str]] = None

    @field_validator("trigger_type")
    @classmethod
    def _check_trigger(cls, v):
        if v is not None and v not in TRIGGER_TYPES:
            raise ValueError(f"trigger_type 必须是 {'/'.join(TRIGGER_TYPES)}")
        return v

    @field_validator("misfire_policy")
    @classmethod
    def _check_misfire(cls, v):
        if v is not None and v not in MISFIRE_POLICIES:
            raise ValueError(f"misfire_policy 必须是 {'/'.join(MISFIRE_POLICIES)}")
        return v

    @field_validator("notify_on")
    @classmethod
    def _check_notify_on(cls, v):
        if v is not None and v not in NOTIFY_ONS:
            raise ValueError(f"notify_on 必须是 {'/'.join(NOTIFY_ONS)}")
        return v

    @field_validator("notify_channels")
    @classmethod
    def _check_channels(cls, v):
        if v is None:
            return v
        bad = [c for c in v if c not in NOTIFY_CHANNELS]
        if bad:
            raise ValueError(f"notify_channels 仅支持 {'/'.join(NOTIFY_CHANNELS)}")
        return list(dict.fromkeys(v))


class SchedulePreviewRequest(BaseModel):
    """cron 预览：不落地，仅校验并计算未来触发时间"""
    trigger_type: str = "cron"
    cron_expr: Optional[str] = None
    run_at: Optional[datetime] = None
    timezone: str = "Asia/Shanghai"
    count: int = Field(3, ge=1, le=10)
