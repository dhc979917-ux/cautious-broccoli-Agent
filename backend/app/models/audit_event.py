from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_id = Column(Integer, nullable=True, index=True)
    action = Column(String(80), nullable=False, index=True)
    resource_type = Column(String(40), nullable=False, index=True)
    resource_id = Column(String(80), nullable=True)
    result = Column(String(20), nullable=False)
    http_status = Column(Integer, nullable=True)
    ip = Column(String(45), nullable=True)
    request_id = Column(String(80), nullable=False, index=True)
    trace_id = Column(String(80), nullable=True)
    changes_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
