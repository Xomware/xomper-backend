"""
Per-request CORS origin
=======================
Every route is an AWS_PROXY integration, so the browser sees the Lambda's own
`Access-Control-Allow-Origin`. The response builders hardcode Xomper's origin;
`handle_errors` passes every response through `echo_allowed_origin` so CLT
Dynasty's site, which calls the same API, gets its own origin back.

A request from any origin outside `ALLOWED_ORIGINS`, or with no Origin header,
gets the response exactly as built. That keeps Xomper's responses
byte-identical, including the absence of `Vary: Origin`.
"""
from __future__ import annotations

from typing import Any

DEFAULT_ORIGIN = "https://xomper.xomware.com"
ALLOWED_ORIGINS = frozenset({DEFAULT_ORIGIN, "https://clt.dynasty.xomware.com"})

_ALLOW_ORIGIN = "Access-Control-Allow-Origin"


def request_origin(event: Any) -> str:
    """The request's Origin header, matched case-insensitively.

    API Gateway REST passes header names as the client sent them, in both
    `headers` and `multiValueHeaders`.
    """
    if not isinstance(event, dict):
        return ""
    for key in ("headers", "multiValueHeaders"):
        for name, value in (event.get(key) or {}).items():
            if name.lower() != "origin":
                continue
            if isinstance(value, list):
                value = value[0] if value else ""
            return value or ""
    return ""


def echo_allowed_origin(response: Any, event: Any) -> Any:
    """Return `response` with the request's origin echoed if it is allowed.

    The headers dict is copied, never mutated: the response builders hand
    out one shared module-level dict, and changing it would leak one
    request's origin into every later warm invocation.
    """
    if not isinstance(response, dict):
        return response
    headers = response.get("headers")
    if not isinstance(headers, dict) or headers.get(_ALLOW_ORIGIN) != DEFAULT_ORIGIN:
        return response

    origin = request_origin(event)
    if origin == DEFAULT_ORIGIN or origin not in ALLOWED_ORIGINS:
        return response

    return {**response, "headers": {**headers, _ALLOW_ORIGIN: origin, "Vary": "Origin"}}
