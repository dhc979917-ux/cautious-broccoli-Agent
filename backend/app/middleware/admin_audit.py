"""Reserve an audit record before administrative work and close it afterwards."""

import logging
import re
from uuid import uuid4

from opentelemetry import trace

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.audit_service import begin_event, complete_event, record_audit_failure


logger = logging.getLogger(__name__)
TRACKED = ("users", "models", "agents", "skills", "mcp", "admin")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
NUMERIC_ID = re.compile(r"/(\d+)(?:/|$)")


def audit_resource(request: Request) -> str | None:
    parts = request.url.path.strip("/").split("/")
    if len(parts) < 3 or parts[:2] != ["api", "v1"] or parts[2] not in TRACKED:
        return None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        return parts[3] if parts[2] == 'admin' and len(parts) > 3 else parts[2]
    if request.method == "GET" and len(parts) > 3 and parts[2] == "admin" and parts[3] in ("memories", "audit", "people", "operations"):
        return parts[3]
    if request.method == "GET" and parts[2] == "users" and parts[3:] in (
        ["me", "mail-password"],
    ):
        return "credentials"
    return None


class AdminAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resource_type = audit_resource(request)
        if not resource_type:
            return await call_next(request)
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if REQUEST_ID.fullmatch(incoming) else uuid4().hex
        span = trace.get_current_span()
        context = span.get_span_context()
        trace_id = f"{context.trace_id:032x}" if context.is_valid else None
        if span.is_recording():
            span.set_attribute("request.id", request_id)
        parts = request.url.path.strip('/').split('/')
        resource_id = next((part for part in parts[3:] if re.fullmatch(r'\d+|[a-f0-9-]{36}', part)), None)
        action = f'{request.method.lower()}_{resource_type}'
        if request.method == 'POST' and parts[-1] in ('publish', 'rollback', 'retry', 'lifecycle', 'review'):
            action = parts[-1] + '_' + resource_type
        request.state.request_id = request_id
        try:
            event_id = begin_event(
                action=action,
                resource_type=resource_type, resource_id=resource_id,
                request_id=request_id, trace_id=trace_id,
                ip=request.client.host if request.client else None,
            )
        except Exception:
            record_audit_failure()
            logger.error("Administrative audit unavailable; operation refused")
            return JSONResponse(
                {"detail": "Audit storage unavailable", "request_id": request_id},
                status_code=503, headers={"X-Request-ID": request_id},
            )
        try:
            response = await call_next(request)
        except Exception:
            self._close(event_id, request, 500)
            raise
        if response.status_code == 403 and not request.headers.get("authorization"):
            response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        recorded = self._close(event_id, request, response.status_code)
        protected_model_test = request.url.path == "/api/v1/admin/models/tests"
        if not recorded and (resource_type in ("memories", "credentials", "audit", "people", "operations")
                             or protected_model_test):
            response = JSONResponse({"detail": "Audit storage unavailable"}, status_code=503)
        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def _close(event_id: int, request: Request, status_code: int) -> bool:
        try:
            complete_event(
                event_id, actor_id=getattr(request.state, "audit_actor_id", None),
                http_status=status_code,
                changes=getattr(request.state, "audit_changes", None),
            )
            return True
        except Exception:
            record_audit_failure()
            logger.error("Administrative audit outcome unavailable; reserved attempt remains")
            return False
