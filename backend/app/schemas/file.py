from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional, List


'''文件中心相关 Schema'''


class FileResponse(BaseModel):
    id: int
    user_id: int
    username: Optional[str] = None        # 管理员视图填充；普通用户视角可省略
    kind: str                             # upload / output
    original_name: str
    stored_name: str
    size: int
    content_type: Optional[str] = None
    session_id: Optional[int] = None
    agent_id: Optional[int] = None
    session_title: Optional[str] = None   # 溯源展示（产出文件）
    # 来源会话属主（管理员判断能否预览他人会话）；会话已删除时为 None
    session_owner_id: Optional[int] = None
    # 来源会话是否已被删除：True 时前端把"来源"置灰且不可点击（标题仍照常展示）
    session_deleted: bool = False
    agent_name: Optional[str] = None
    created_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class FileRenameRequest(BaseModel):
    original_name: str


class BatchDownloadRequest(BaseModel):
    file_ids: List[int]
