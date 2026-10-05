"""
GET /clt/me - The caller's CLT member row and linked Sleeper account.

No body. Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_member`, which 403s anyone not on the roster.

`member.sleeperUserId` is the roster mapping carried over from Supabase.
`linkedSleeperUserId` is the account the caller linked through
/me/sleeper-link. The two are reported separately so the frontend can warn
when they disagree.

`isAdmin` is Xomper's admin gate (Cognito `admin` group plus an admin
whitelisted_users row), the same check the CLT admin routes enforce.
"""
from __future__ import annotations

from typing import Any

from lambdas.common import platform_users
from lambdas.common.admin_gate import is_admin
from lambdas.common.clt_gate import require_member
from lambdas.common.errors import handle_errors
from lambdas.common.utility_helpers import success_response

HANDLER = "api_clt_me"


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    member = require_member(event)
    record = platform_users.get_user(member["sub"]) or {}

    return success_response({
        "member": {
            "email": member["email"],
            "displayName": member.get("displayName", ""),
            "role": member.get("role", ""),
            "sleeperUserId": member.get("sleeperUserId", ""),
        },
        "linkedSleeperUserId": record.get("sleeperUserId", ""),
        "isAdmin": is_admin(event),
    })
