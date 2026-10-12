"""站内通知 API（当前仅调度任务执行结果）"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.database import get_db
from app.models.notification import Notification
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get("/")
def list_notifications(limit: int = Query(20, ge=1, le=100),
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    q = (db.query(Notification)
         .filter(Notification.user_id == current_user.id)
         .order_by(Notification.id.desc()))
    unread = q.filter(Notification.is_read == False).count()  # noqa: E712
    items = q.limit(limit).all()
    return {
        "unread": unread,
        "items": [{
            "id": n.id, "type": n.type, "title": n.title, "content": n.content,
            "link": n.link, "job_id": n.job_id, "run_id": n.run_id,
            "is_read": n.is_read, "created_at": n.created_at,
        } for n in items],
    }


@router.patch("/read-all")
def mark_all_read(db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    (db.query(Notification)
     .filter(Notification.user_id == current_user.id,
             Notification.is_read == False)  # noqa: E712
     .update({"is_read": True}))
    db.commit()
    return {"ok": True}


@router.patch("/{nid}/read")
def mark_read(nid: int, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    row = (db.query(Notification)
           .filter(Notification.id == nid,
                   Notification.user_id == current_user.id).first())
    if not row:
        raise HTTPException(status_code=404, detail="通知不存在")
    row.is_read = True
    db.commit()
    return {"ok": True}
