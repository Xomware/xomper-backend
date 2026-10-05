"""
XOMPER Error Classes
====================
Standardized error handling for all Lambda functions.

Features:
- Consistent error response format
- HTTP status codes
- Easy to catch and handle
- Serializable for API responses
"""

import json
import functools
import re
import traceback
from typing import Optional
from lambdas.common.cors import echo_allowed_origin
from lambdas.common.logger import get_logger

log = get_logger(__file__)

# Email masking pattern for PII protection in logs
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def _mask_emails_in_string(text: str) -> str:
    """Replace email addresses in a string with masked versions."""
    def _mask(match: re.Match) -> str:
        email = match.group(0)
        local, domain = email.rsplit('@', 1)
        return f"{local[0]}***@{domain}" if local else f"***@{domain}"
    return _EMAIL_RE.sub(_mask, text)


class XomperError(Exception):
    """
    Base exception class for all Xomper errors.

    Usage:
        raise XomperError("Something went wrong", status=400)

    Or catch and convert to response:
        except XomperError as e:
            return e.to_response()
    """

    def __init__(
        self,
        message: str,
        handler: str = "unknown",
        function: str = "unknown",
        status: int = 500,
        details: Optional[dict] = None
    ):
        self.message = message
        self.handler = handler
        self.function = function
        self.status = status
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Convert error to dictionary for JSON response."""
        return {
            "error": {
                "message": self.message,
                "handler": self.handler,
                "function": self.function,
                "status": self.status,
                **self.details
            }
        }

    def to_response(self, is_api: bool = True) -> dict:
        """Convert error to Lambda response format."""
        body = self.to_dict()
        return {
            "statusCode": self.status,
            "headers": {
                "Access-Control-Allow-Origin": "https://xomper.xomware.com",
                "Content-Type": "application/json"
            },
            "body": json.dumps(body) if is_api else body,
            "isBase64Encoded": False
        }

    def log_error(self) -> None:
        """Log the error with full context, masking PII."""
        safe_message = _mask_emails_in_string(self.message)
        log.error(f"{self.__class__.__name__} in {self.handler}.{self.function}: {safe_message}")
        if self.details:
            safe_details = _mask_emails_in_string(str(self.details))
            log.error(f"   Details: {safe_details}")

    def __str__(self) -> str:
        return json.dumps(self.to_dict())


# ============================================
# Specific Error Types
# ============================================

class AuthorizationError(XomperError):
    """Raised when authorization fails."""

    def __init__(self, message: str = "Unauthorized", handler: str = "authorizer", function: str = "unknown"):
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=401
        )


class ValidationError(XomperError):
    """Raised when input validation fails."""

    def __init__(self, message: str, handler: str = "unknown", function: str = "unknown", field: Optional[str] = None):
        details = {"field": field} if field else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=400,
            details=details
        )


class NotFoundError(XomperError):
    """Raised when a resource is not found."""

    def __init__(self, message: str, handler: str = "unknown", function: str = "unknown", resource: Optional[str] = None):
        details = {"resource": resource} if resource else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=404,
            details=details
        )


class DynamoDBError(XomperError):
    """Raised when DynamoDB operations fail."""

    def __init__(self, message: str, handler: str = "dynamo_helpers", function: str = "unknown", table: Optional[str] = None):
        details = {"table": table} if table else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=500,
            details=details
        )


class SleeperAPIError(XomperError):
    """Raised when Sleeper API calls fail."""

    def __init__(self, message: str, handler: str = "sleeper", function: str = "unknown", endpoint: Optional[str] = None):
        details = {"endpoint": endpoint} if endpoint else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=502,
            details=details
        )


class DraftNotCompleteError(XomperError):
    """Raised when the post-draft AI Review trigger fires before the
    Sleeper draft has reached `status == complete`. Surfaces as HTTP
    412 (Precondition Failed) to the admin client."""

    def __init__(
        self,
        message: str = "Sleeper draft is not yet complete",
        handler: str = "notif_ai_review_postdraft",
        function: str = "run",
        draft_status: Optional[str] = None,
    ):
        details = {"draft_status": draft_status} if draft_status else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=412,
            details=details,
        )


class PreseasonWindowPassedError(XomperError):
    """Raised when the preseason AI Review trigger fires after the
    regular-season window has already started (i.e. NFL state's
    `season_type` is `regular` or `post`). Surfaces as HTTP 412
    (Precondition Failed) to the admin client."""

    def __init__(
        self,
        message: str = "Preseason window has already passed",
        handler: str = "notif_ai_review_preseason",
        function: str = "run",
        nfl_season: Optional[str] = None,
        season_type: Optional[str] = None,
    ):
        details: dict = {}
        if nfl_season:
            details["nfl_season"] = nfl_season
        if season_type:
            details["season_type"] = season_type
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=412,
            details=details,
        )


class DoNotBroadcastError(XomperError):
    """Raised when an AI Review broadcast path attempts to fan out a
    report whose `metadata.do_not_broadcast == "true"` flag is set.

    Surfaces as HTTP 409 (Conflict) so the admin client can flip the
    Broadcast button label and message the user that the flag must be
    toggled off before sending. Distinct from
    `ReportAlreadyExistsError` (same status code) so the iOS surface
    can render a tailored error string.
    """

    def __init__(
        self,
        message: str = (
            "Report is marked do_not_broadcast. "
            "Toggle the flag off to broadcast."
        ),
        handler: str = "ai_reports_broadcast",
        function: str = "run",
        report_type: Optional[str] = None,
        period: Optional[str] = None,
    ):
        details: dict = {}
        if report_type:
            details["report_type"] = report_type
        if period:
            details["period"] = period
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=409,
            details=details,
        )


class ReportAlreadyExistsError(XomperError):
    """Raised when an AI Review report already exists for a given
    `(league_id, report_type, period)` and the caller did not pass
    `force=True`. Surfaces as HTTP 409 (Conflict) so the admin client
    can flip its button label to 'Regenerate (force)'."""

    def __init__(
        self,
        message: str = "Report already exists",
        handler: str = "notif_ai_review_postdraft",
        function: str = "run",
        existing: Optional[dict] = None,
    ):
        details = {"existing": existing} if existing else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=409,
            details=details,
        )


class ClaudeAPIError(XomperError):
    """Raised when Anthropic / Claude API calls fail terminally
    (after retries are exhausted, or on non-retriable errors)."""

    def __init__(
        self,
        message: str,
        handler: str = "claude_helper",
        function: str = "generate",
        model: Optional[str] = None,
    ):
        details = {"model": model} if model else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=502,
            details=details,
        )


class MemoryStoreError(XomperError):
    """Raised when the `xomper-ai-memories` Dynamo store fails on
    read/write/delete. Distinct from `DynamoDBError` so the weekly
    orchestrator can isolate memory-layer failures (which it can
    swallow without losing the recap) from report-layer failures
    (which are terminal)."""

    def __init__(
        self,
        message: str,
        handler: str = "ai_memories_store",
        function: str = "unknown",
        table: Optional[str] = None,
    ):
        details = {"table": table} if table else {}
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=500,
            details=details,
        )


class EmailError(XomperError):
    """Raised when email processing fails."""

    def __init__(self, message: str, handler: str = "email", function: str = "unknown"):
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=500
        )


class SSMError(XomperError):
    """Raised when SSM parameter operations fail."""

    def __init__(self, message: str, handler: str = "ssm_helpers", function: str = "unknown"):
        super().__init__(
            message=message,
            handler=handler,
            function=function,
            status=500
        )


# ============================================
# Sensitive Data Masking
# ============================================

SENSITIVE_FIELDS = {
    'refreshToken', 'refresh_token',
    'accessToken', 'access_token',
    'password', 'passwd',
    'secret', 'apiKey', 'api_key',
    'authorization', 'Authorization',
    'x-api-key', 'X-API-Key',
    'sessionToken', 'session_token',
    'privateKey', 'private_key',
    'clientSecret', 'client_secret'
}


def mask_sensitive_data(data: object, mask_value: str = "***MASKED***") -> object:
    """
    Recursively mask sensitive fields in dictionaries and lists.

    Args:
        data: Dict, list, or other data structure
        mask_value: String to replace sensitive values with

    Returns:
        Masked copy of data
    """
    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            if key in SENSITIVE_FIELDS or any(sensitive.lower() in key.lower() for sensitive in ['token', 'password', 'secret', 'key', 'auth']):
                masked[key] = mask_value
            else:
                masked[key] = mask_sensitive_data(value, mask_value)
        return masked
    elif isinstance(data, list):
        return [mask_sensitive_data(item, mask_value) for item in data]
    elif isinstance(data, str):
        # Mask any email addresses in string values
        result = _mask_emails_in_string(data)
        if len(result) > 100:
            return result[:50] + "...[truncated]..." + result[-20:]
        return result
    else:
        return data


def log_error_context(handler_name: str, function_name: str, event: dict, context: object = None) -> None:
    """
    Log relevant context information when an error occurs.

    Args:
        handler_name: Name of the handler
        function_name: Name of the function
        event: Lambda event dict
        context: Lambda context object
    """
    try:
        http_method = event.get('httpMethod', event.get('requestContext', {}).get('http', {}).get('method', 'N/A'))
        path = event.get('path', event.get('rawPath', 'N/A'))
        query_params = mask_sensitive_data(event.get('queryStringParameters', {}))
        headers = mask_sensitive_data(event.get('headers', {}))

        log.error(f"Error Context for {handler_name}.{function_name}:")
        log.error(f"   Method: {http_method}")
        log.error(f"   Path: {path}")
        log.error(f"   Query Params: {query_params}")
        log.error(f"   Headers: {headers}")

        if event.get('body'):
            try:
                body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
                safe_body = mask_sensitive_data(body)
                log.error(f"   Body: {safe_body}")
            except (json.JSONDecodeError, TypeError):
                log.error("   Body: [Unable to parse]")

        if context:
            log.error(f"   Request ID: {getattr(context, 'aws_request_id', 'N/A')}")
            log.error(f"   Function: {getattr(context, 'function_name', 'N/A')}")

    except Exception as log_err:
        log.error(f"Error while logging context: {log_err}")


# ============================================
# Error Handler Decorator
# ============================================

def handle_errors(handler_name: str, log_context: bool = True):
    """
    Decorator to handle errors consistently across handlers.

    Args:
        handler_name: Name of the handler for logging
        log_context: If True, logs event/context details on error (with sensitive data masked)

    Usage:
        @handle_errors("email")
        def handler(event, context):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(event, context):
            return echo_allowed_origin(_run(event, context), event)

        def _run(event, context):
            try:
                return func(event, context)
            except XomperError as e:
                e.log_error()
                if log_context:
                    log_error_context(handler_name, func.__name__, event, context)
                return e.to_response()
            except Exception as e:
                safe_message = _mask_emails_in_string(str(e))
                log.error(f"Unexpected error in {handler_name}: {safe_message}")
                log.error(traceback.format_exc())

                if log_context:
                    log_error_context(handler_name, func.__name__, event, context)

                error = XomperError(
                    message=str(e),
                    handler=handler_name,
                    function=func.__name__,
                    status=500
                )
                return error.to_response()
        return wrapper
    return decorator
