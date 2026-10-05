"""
CLT roster management, admin only. One Lambda, two routes:

    GET  /clt/members-list    every roster row, active or not
    POST /clt/members-update  {"email": "<current>", "newEmail"?, "displayName"?,
                               "active"?, "clearSub"?: true}

Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_clt_admin`: an active member who also passes Xomper's admin
gate.

The row is bound to a Cognito `sub` at first sign-in (see clt_gate), so a
member who signs in with a different Google account is refused until the
binding goes. `clearSub` drops it so the next sign-in rebinds. Changing the
email always drops it: the new address is a new account.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from lambdas.common.clt_gate import list_members, normalize_email, require_clt_admin
from lambdas.common.constants import CLT_MEMBERS_TABLE
from lambdas.common.errors import NotFoundError, ValidationError, XomperError, handle_errors
from lambdas.common.ses_helper import validate_email
from lambdas.common.utility_helpers import parse_body, success_response

HANDLER = "api_clt_members"
DISPLAY_NAME_MAX = 50


def _table() -> Any:
    return boto3.resource("dynamodb").Table(CLT_MEMBERS_TABLE)


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": row["email"],
        "displayName": row.get("displayName", ""),
        "role": row.get("role", ""),
        "sleeperUserId": row.get("sleeperUserId", ""),
        "active": bool(row.get("active")),
        "boundToAccount": bool(row.get("sub")),
    }


def _bad(message: str, field: str) -> ValidationError:
    return ValidationError(message, handler=HANDLER, function="_update", field=field)


def _list(event: dict[str, Any]) -> dict[str, Any]:
    rows = sorted(list_members(), key=lambda r: r.get("displayName", "").lower())
    return success_response({"members": [_shape(r) for r in rows]})


def _changes(body: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "displayName" in body:
        name = body["displayName"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > DISPLAY_NAME_MAX:
            raise _bad(f"displayName must be 1-{DISPLAY_NAME_MAX} characters", "displayName")
        out["displayName"] = name.strip()
    if "active" in body:
        if not isinstance(body["active"], bool):
            raise _bad("active must be true or false", "active")
        out["active"] = body["active"]
    if "clearSub" in body:
        if body["clearSub"] is not True:
            raise _bad("clearSub can only be true", "clearSub")
        out.pop("sub", None)
    if "newEmail" in body:
        email = body["newEmail"]
        if not isinstance(email, str) or not validate_email(email):
            raise _bad("newEmail is not a valid email", "newEmail")
        out["email"] = normalize_email(email)
        out.pop("sub", None)
    if out == row:
        raise _bad("nothing to change", "body")
    out["updatedAt"] = datetime.now(timezone.utc).isoformat()
    return out


def _update(event: dict[str, Any]) -> dict[str, Any]:
    body = parse_body(event)
    email = body.get("email")
    if not isinstance(email, str) or not email.strip():
        raise _bad("email is required", "email")
    email = normalize_email(email)

    row = _table().get_item(Key={"email": email}).get("Item")
    if not row:
        raise NotFoundError("member not found", handler=HANDLER, function="_update", resource="member")
    updated = _changes(body, row)

    if updated["email"] == email:
        _table().put_item(Item=updated)
        return success_response({"member": _shape(updated)})

    # The email is the key, so a new address is a new item. One transaction,
    # so a failure never leaves the member on both rows or on neither.
    try:
        _table().meta.client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": CLT_MEMBERS_TABLE,
                "Item": updated,
                "ConditionExpression": "attribute_not_exists(email)",
            }},
            {"Delete": {
                "TableName": CLT_MEMBERS_TABLE,
                "Key": {"email": email},
                "ConditionExpression": "attribute_exists(email)",
            }},
        ])
    except ClientError as err:
        if err.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        raise XomperError(
            "another member already has that email",
            handler=HANDLER,
            function="_update",
            status=409,
        ) from err
    return success_response({"member": _shape(updated)})


_ROUTES = {
    "members-list": (_list, "GET"),
    "members-update": (_update, "POST"),
}


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    require_clt_admin(event)

    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path") or event.get("resource") or ""
    action, expected = _ROUTES.get(path.rstrip("/").rsplit("/", 1)[-1], (None, ""))
    if action is None or expected != method:
        raise ValidationError(f"Unsupported route: {method} {path}", handler=HANDLER)

    return action(event)
