"""Create role/user model-policy table without modifying existing grants."""

from app.database import engine
from app.models.model_access_policy import ModelAccessPolicy


if __name__ == "__main__":
    ModelAccessPolicy.__table__.create(bind=engine, checkfirst=True)
