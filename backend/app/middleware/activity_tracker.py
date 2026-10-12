# -*- coding: utf-8 -*-
"""Activity tracking middleware: updates user.last_active every 5 minutes.

Lightweight: reads JWT from Authorization header, throttles DB writes in-memory
(one write per user per 5 min), never blocks or fails the request.
"""
import time
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

THROTTLE_SECONDS = 300  # 5 minutes
_throttle: dict[int, float] = {}  # user_id -> last_update_timestamp


class ActivityTrackerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return response
            token = auth[7:]
            from app.core.security import decode_token
            payload = decode_token(token)
            if not payload:
                return response
            user_id = int(payload.get("sub", 0))
            if not user_id:
                return response
            now_ts = time.time()
            if now_ts - _throttle.get(user_id, 0) < THROTTLE_SECONDS:
                return response
            _throttle[user_id] = now_ts
            from app.database import SessionLocal
            from app.models.user import User
            db = SessionLocal()
            try:
                db.query(User).filter(User.id == user_id).update(
                    {"last_active": datetime.now()}
                )
                db.commit()
            finally:
                db.close()
        except Exception:
            pass  # never fail the request over activity tracking
        return response
