"""
CLT league settings. One Lambda, two routes:

    GET  /clt/settings         any member
    POST /clt/settings-update  {"emailNotifications": true | false}  admin

Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_member`, plus Xomper's admin gate for the update.

`emailNotifications` gates CLT's own emails (new proposal, status change,
steal request). It reads false until an admin turns it on.
"""
from __future__ import annotations

from typing import Any

from lambdas.common import clt_settings
from lambdas.common.clt_gate import require_clt_admin, require_member
from lambdas.common.errors import ValidationError, handle_errors
from lambdas.common.utility_helpers import parse_body, success_response

HANDLER = "api_clt_settings"


def _get(event: dict[str, Any]) -> dict[str, Any]:
    return success_response({"emailNotifications": clt_settings.email_notifications_on()})


def _update(event: dict[str, Any]) -> dict[str, Any]:
    require_clt_admin(event)
    on = parse_body(event).get("emailNotifications")
    if not isinstance(on, bool):
        raise ValidationError(
            "emailNotifications must be true or false",
            handler=HANDLER,
            function="_update",
            field="emailNotifications",
        )
    clt_settings.set_email_notifications(on)
    return success_response({"emailNotifications": on})


_ROUTES = {
    "settings": (_get, "GET"),
    "settings-update": (_update, "POST"),
}


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    require_member(event)

    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path") or event.get("resource") or ""
    action, expected = _ROUTES.get(path.rstrip("/").rsplit("/", 1)[-1], (None, ""))
    if action is None or expected != method:
        raise ValidationError(f"Unsupported route: {method} {path}", handler=HANDLER)

    return action(event)
