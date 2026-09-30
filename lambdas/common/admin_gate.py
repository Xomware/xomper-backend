"""
Admin gate
==========
Role enforcement for admin-only API endpoints, on top of the JWT check the
authorizer already did.

Identity comes only from the authorizer context, never from the request.
Admin needs both:
- `admin` in the verified `cognito:groups` claim. Groups are managed by pool
  admins, so a user cannot grant one to themselves.
- an active `whitelisted_users` row for the token's email with `is_admin`,
  which stays the per-app revocation switch and supplies the row handlers
  use as the audit actor.

The email claim alone is not proof: the pool lets users change their own
email without re-verifying (`AttributesRequireVerificationBeforeUpdate` is
empty), and Sleeper ids are public. The group check is what makes the
email lookup safe.
"""
from __future__ import annotations

from typing import Any

from lambdas.common.caller_identity import get_caller
from lambdas.common.logger import get_logger
from lambdas.common.supabase_helper import get_whitelisted_user_by_email

log = get_logger(__file__)


class NotAdmin(Exception):
    """Raised when an admin endpoint is hit by a non-admin user."""


def require_admin(event: dict[str, Any], body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the caller's `whitelisted_users` row if they are an admin.

    `body` is accepted for existing call sites and ignored: nothing the
    caller sends decides who they are.

    Raises AuthorizationError (401) when the request has no verified caller,
    NotAdmin otherwise.
    """
    caller = get_caller(event)

    if not caller.is_admin:
        log.warning(f"admin_gate: caller not in admin group (sub={caller.user_id})")
        raise NotAdmin("not authorized")

    user = get_whitelisted_user_by_email(caller.email) if caller.email else None
    if not user or not user.get("is_admin"):
        log.warning(f"admin_gate: no admin whitelisted_users row (sub={caller.user_id})")
        raise NotAdmin("not authorized")

    return user


def is_admin(event: dict[str, Any], body: dict[str, Any] | None = None) -> bool:
    """Non-raising `require_admin` for read paths that only branch on it.

    `api_ai_reports_latest` and `api_ai_reports_list` serve everyone and
    filter redacted rows for non-admins, so a failed lookup must degrade to
    "not admin" rather than fail the request.
    """
    try:
        require_admin(event)
    except Exception as err:  # noqa: BLE001 — read paths never fail on identity
        log.info(f"admin_gate.is_admin: not admin ({type(err).__name__})")
        return False
    return True
