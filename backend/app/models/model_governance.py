from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, Numeric, String
from sqlalchemy.sql import func

from app.database import Base


class ModelMetadata(Base):
    __tablename__ = "model_metadata"

    model_id = Column(String(200), primary_key=True)
    display_name = Column(String(200), nullable=True)
    purpose = Column(String(20), nullable=True)
    capabilities = Column(JSON, nullable=True)
    context_length = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    input_price_per_million = Column(Numeric(14, 6), nullable=True)
    output_price_per_million = Column(Numeric(14, 6), nullable=True)
    cached_input_price_per_million = Column(Numeric(14, 6), nullable=True)
    price_source = Column(String(300), nullable=True)
    price_updated_at = Column(DateTime(timezone=True), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GatewayModelEntry(Base):
    __tablename__ = "gateway_model_entries"

    model_id = Column(String(200), primary_key=True)
    provider = Column(String(120), nullable=True)
    seen_at = Column(DateTime(timezone=True), nullable=False)


class ModelTestRun(Base):
    __tablename__ = "model_test_runs"

    id = Column(Integer, primary_key=True)
    model_id = Column(String(200), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    trace_id = Column(String(36), unique=True, nullable=False)
    outcome = Column(String(24), nullable=False)
    error_code = Column(String(32), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    tested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
