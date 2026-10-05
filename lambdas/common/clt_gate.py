"""
CLT member gate
===============
Every /clt/* handler calls `require_member` first. xomper-client tokens get
Allow on the whole stage, so this, not the authorizer, is what keeps a Xomper
user who is not on CLT's roster out of CLT's routes.

The roster is `CLT_MEMBERS_TABLE`, keyed by lowercased email.

The email claim alone is not proof: the pool lets users change their own email
without re-verifying (see admin_gate.py). So the first sign-in binds the row to
the caller's `sub`, and from then on a different `sub` presenting that email is
refused. An admin clears `sub` when a member legitimately switches accounts.
"""
from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError

from lambdas.common.caller_identity import get_caller
from lambdas.common.constants import CLT_MEMBERS_TABLE
from lambdas.common.errors import DynamoDBError, XomperError
from lambdas.common.logger import get_logger

log = get_logger(__file__)


class NotMember(XomperError):
    """403 for a verified caller who is not an active CLT member."""

    def __init__(self) -> None:
        super().__init__(
            message="not on the CLT roster",
            handler="clt_gate",
            function="require_member",
            status=403,
        )


def _table() -> Any:
    return boto3.resource("dynamodb").Table(CLT_MEMBERS_TABLE)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def require_member(event: dict[str, Any]) -> dict[str, Any]:
    """Return the caller's active member row, binding it to their sub on first use.

    Raises AuthorizationError (401) with no verified caller, NotMember (403)
    when the caller is not an active member or the row belongs to another sub.
    """
    caller = get_caller(event)
    email = normalize_email(caller.email)
    if not email:
        raise NotMember()

    try:
        member = _table().get_item(Key={"email": email}).get("Item")
    except ClientError as err:
        raise DynamoDBError(f"require_member read failed: {err}", table=CLT_MEMBERS_TABLE) from err

    if not member or not member.get("active"):
        log.warning(f"clt_gate: not an active member (sub={caller.user_id})")
        raise NotMember()

    if member.get("sub") == caller.user_id:
        return member

    try:
        _table().update_item(
            Key={"email": email},
            UpdateExpression="SET #sub = :sub",
            ConditionExpression="attribute_not_exists(#sub) OR #sub = :sub",
            ExpressionAttributeNames={"#sub": "sub"},
            ExpressionAttributeValues={":sub": caller.user_id},
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.warning(f"clt_gate: member row bound to another sub (sub={caller.user_id})")
            raise NotMember() from err
        raise DynamoDBError(f"require_member bind failed: {err}", table=CLT_MEMBERS_TABLE) from err

    log.info(f"clt_gate: bound member row to sub={caller.user_id}")
    member["sub"] = caller.user_id
    return member
