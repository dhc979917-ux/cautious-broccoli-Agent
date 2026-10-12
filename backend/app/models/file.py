from sqlalchemy import Column, Integer, BigInteger, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from datetime import datetime
from app.database import Base


'''文件元数据表（文件中心）

内容存本地磁盘，元数据全部落库：
  - 上传文件存 uploads/{stored_name}
  - 技能产出文件存 outputs/user_{user_id}/{stored_name 相对路径}
列表/搜索/统计/权限全部走 SQL，按 stored_name 读本地磁盘。
'''


class FileRecord(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True, index=True)
    # 归属用户（权限隔离的核心字段：普通用户查询强制 user_id=本人）
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # 文件类别：upload=用户上传 / output=技能执行产出
    kind = Column(String(20), nullable=False, index=True)
    # 展示名（用户上传的原始文件名 / 产出文件名；可重命名）
    original_name = Column(String(255), nullable=False)
    # 磁盘上的实际文件名（uploads 目录含 {user_id}_{ts}_ 前缀；outputs 目录为纯文件名）
    stored_name = Column(String(255), nullable=False, index=True)
    size = Column(BigInteger, default=0)
    content_type = Column(String(100), nullable=True)
    # 溯源：产出文件来自哪个会话/哪个 Agent（上传文件可为空）
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    # 来源会话标题快照：会话被物理删除后 session_id 仍保留（用于判定"已删除"），
    # 标题从这里取，保证文件中心"来源"列的展示不因删会话而消失
    session_title = Column(String(200), nullable=True)
    # 统一北京时间：Python 端 default 优先生效（SQLite 的 func.now() 存的是 UTC，会差 8 小时）
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    # 软删除时间（回收站）；NULL=正常，非 NULL=在回收站
    deleted_at = Column(DateTime(timezone=True), nullable=True)
