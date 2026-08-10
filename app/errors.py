"""Stable error envelope and the application exception hierarchy.

Every error the API returns has the same shape and a machine-readable code that
is part of the public contract:

.. code-block:: json

    {
      "error": {
        "code": "permission_denied",
        "message": "You do not have permission to perform this action.",
        "details": [],
        "correlation_id": "01J..."
      }
    }

Codes are stable across releases. Messages are for humans and may be reworded;
integrators branch on ``code``, never on ``message``.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

__all__ = [
    "AtlasError",
    "AuthenticationRequired",
    "BusinessRuleViolation",
    "Conflict",
    "ErrorCode",
    "IdempotencyConflict",
    "IntegrationFailure",
    "InternalError",
    "MFARequired",
    "NotFound",
    "PermissionDenied",
    "PreconditionFailed",
    "RateLimited",
    "ReauthenticationRequired",
    "ServiceUnavailable",
    "TenantIsolationViolation",
    "ValidationFailed",
    "error_payload",
    "register_error_handlers",
]


class ErrorCode:
    """Canonical, stable error codes. Treated as a public API surface."""

    VALIDATION_FAILED = "validation_failed"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_CREDENTIALS = "invalid_credentials"
    MFA_REQUIRED = "mfa_required"
    MFA_INVALID = "mfa_invalid"
    REAUTHENTICATION_REQUIRED = "reauthentication_required"
    ACCOUNT_LOCKED = "account_locked"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PRECONDITION_FAILED = "precondition_failed"
    BUSINESS_RULE_VIOLATION = "business_rule_violation"
    PERIOD_CLOSED = "period_closed"
    LEDGER_UNBALANCED = "ledger_unbalanced"
    IMMUTABLE_RECORD = "immutable_record"
    TENANT_ISOLATION_VIOLATION = "tenant_isolation_violation"
    RATE_LIMITED = "rate_limited"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INTEGRATION_FAILURE = "integration_failure"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


class AtlasError(Exception):
    """Base class for every error Atlas raises deliberately.

    Anything not derived from this is an unexpected failure: it is logged with a
    stack trace and reported to the client as a generic internal error, never
    with the original message.
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "An unexpected error occurred."
    #: Whether the raised message is safe to show to an end user.
    public_message: bool = True

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or []
        self.headers = headers or {}
        super().__init__(self.message)

    def to_dict(self, correlation_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message if self.public_message else AtlasError.message,
            "details": self.details,
        }
        if correlation_id:
            payload["correlation_id"] = correlation_id
        return {"error": payload}


class ValidationFailed(AtlasError):
    status_code = 422
    code = ErrorCode.VALIDATION_FAILED
    message = "The submitted data failed validation."


class AuthenticationRequired(AtlasError):
    status_code = 401
    code = ErrorCode.AUTHENTICATION_REQUIRED
    message = "Authentication is required to access this resource."


class InvalidCredentials(AtlasError):
    status_code = 401
    code = ErrorCode.INVALID_CREDENTIALS
    # Deliberately identical for unknown-user and wrong-password so the response
    # cannot be used to enumerate accounts.
    message = "The credentials provided are not valid."


class AccountLocked(AtlasError):
    status_code = 423
    code = ErrorCode.ACCOUNT_LOCKED
    message = "This account is temporarily locked after repeated failed attempts."


class MFARequired(AtlasError):
    status_code = 401
    code = ErrorCode.MFA_REQUIRED
    message = "Multi-factor authentication is required to continue."


class MFAInvalid(AtlasError):
    status_code = 401
    code = ErrorCode.MFA_INVALID
    message = "The verification code is not valid."


class ReauthenticationRequired(AtlasError):
    status_code = 403
    code = ErrorCode.REAUTHENTICATION_REQUIRED
    message = "This action requires you to confirm your identity again."


class PermissionDenied(AtlasError):
    status_code = 403
    code = ErrorCode.PERMISSION_DENIED
    message = "You do not have permission to perform this action."


class ApprovalRequired(AtlasError):
    status_code = 403
    code = ErrorCode.APPROVAL_REQUIRED
    message = "This action requires approval before it can take effect."


class NotFound(AtlasError):
    status_code = 404
    code = ErrorCode.NOT_FOUND
    message = "The requested resource was not found."


class Conflict(AtlasError):
    status_code = 409
    code = ErrorCode.CONFLICT
    message = "The request conflicts with the current state of the resource."


class IdempotencyConflict(AtlasError):
    status_code = 409
    code = ErrorCode.IDEMPOTENCY_CONFLICT
    message = "This idempotency key was already used with a different request body."


class PreconditionFailed(AtlasError):
    status_code = 412
    code = ErrorCode.PRECONDITION_FAILED
    message = "The resource has changed since it was last read."


class BusinessRuleViolation(AtlasError):
    status_code = 422
    code = ErrorCode.BUSINESS_RULE_VIOLATION
    message = "This action is not permitted by a business rule."


class TenantIsolationViolation(AtlasError):
    """A cross-tenant access attempt.

    Reported to the client as a plain 404 so that a probe cannot distinguish
    "exists in another tenant" from "does not exist", while the server side logs
    it at CRITICAL as a security event.
    """

    status_code = 404
    code = ErrorCode.NOT_FOUND
    message = "The requested resource was not found."
    public_message = True


class RateLimited(AtlasError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please retry later."


class IntegrationFailure(AtlasError):
    status_code = 502
    code = ErrorCode.INTEGRATION_FAILURE
    message = "An upstream service failed to respond correctly."
    public_message = False


class ServiceUnavailable(AtlasError):
    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    message = "The service is temporarily unavailable."


class InternalError(AtlasError):
    status_code = 500
    code = ErrorCode.INTERNAL_ERROR
    message = "An unexpected error occurred."
    public_message = False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_HTTP_CODE_MAP: dict[int, str] = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.AUTHENTICATION_REQUIRED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.CONFLICT,
    409: ErrorCode.CONFLICT,
    412: ErrorCode.PRECONDITION_FAILED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.INTEGRATION_FAILURE,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}

#: Generic messages for HTTP errors, so werkzeug's default text (which can leak
#: routing details) never reaches a client.
_SAFE_HTTP_MESSAGES: dict[int, str] = {
    400: "The request could not be understood.",
    401: "Authentication is required to access this resource.",
    403: "You do not have permission to perform this action.",
    404: "The requested resource was not found.",
    405: "That method is not allowed for this resource.",
    413: "The uploaded payload is larger than the permitted maximum.",
    415: "The submitted content type is not supported.",
    429: "Too many requests. Please retry later.",
}


def _wants_json() -> bool:
    """Decide between a JSON envelope and an HTML error page."""
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] >= accept["text/html"] and accept["application/json"] > 0


def error_payload(exc: AtlasError, correlation_id: str | None = None) -> dict[str, Any]:
    return exc.to_dict(correlation_id)


def _current_correlation_id() -> str | None:
    from app.middleware import get_correlation_id

    return get_correlation_id()


def _render(exc: AtlasError) -> Response:
    correlation_id = _current_correlation_id()
    if _wants_json():
        response = jsonify(exc.to_dict(correlation_id))
    else:
        try:
            body = render_template(
                "errors/error.html",
                code=exc.status_code,
                error_code=exc.code,
                message=exc.message if exc.public_message else AtlasError.message,
                correlation_id=correlation_id,
            )
        except Exception:  # pragma: no cover - template failure must not mask the error
            body = f"<h1>{exc.status_code}</h1><p>{AtlasError.message}</p>"
        response = Response(body, mimetype="text/html")
    response.status_code = exc.status_code
    for header, value in exc.headers.items():
        response.headers[header] = value
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


def register_error_handlers(app: Flask) -> None:
    """Install the handlers that guarantee a consistent envelope."""

    @app.errorhandler(AtlasError)
    def _handle_atlas_error(exc: AtlasError) -> Response:
        if isinstance(exc, TenantIsolationViolation):
            app.logger.critical(
                "tenant isolation violation",
                extra={"event": "security.tenant_isolation", "detail": str(exc)},
            )
        elif exc.status_code >= 500:
            app.logger.exception("application error", extra={"error_code": exc.code})
        else:
            app.logger.info(
                "handled error",
                extra={"error_code": exc.code, "status": exc.status_code},
            )
        return _render(exc)

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException) -> Response:
        status = exc.code or 500
        wrapped = AtlasError(
            message=_SAFE_HTTP_MESSAGES.get(status, AtlasError.message),
            code=_HTTP_CODE_MAP.get(status, ErrorCode.INTERNAL_ERROR),
            status_code=status,
        )
        if exc.response is not None and status == 429:
            wrapped.headers.update(
                {k: v for k, v in exc.response.headers.items() if k.lower().startswith("retry")}
            )
        return _render(wrapped)

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception) -> Response:
        # The only place a raw traceback is produced - and it goes to the log,
        # never to the client.
        app.logger.exception(
            "unhandled exception",
            extra={"event": "error.unhandled", "exception_type": type(exc).__name__},
        )
        return _render(InternalError())
