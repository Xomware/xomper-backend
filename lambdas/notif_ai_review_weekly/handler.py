"""
Notification — Weekly AI Review (scheduled)
===========================================
EventBridge cron entry point for F3's weekly AI recap. Fires every
Tuesday afternoon during the NFL regular season + playoffs; runs
through the shared `weekly_orchestrator.run_weekly(...)` pipeline.

Triggered by EventBridge scheduled event (no API Gateway). The
orchestrator owns the pre-flight gate (no-op outside the regular /
post season), idempotency, data fetch, Claude generation, memory
append, and email + push fan-out.

Behavior contract:
- Real cron fires set no body — defaults flow (current week,
  broadcast, not forced).
- Off-season fires are absorbed by the orchestrator and return a
  `skipped_offseason` status (NOT an exception — EventBridge would
  retry on raise).
- Claude / Sleeper / Dynamo failures are caught here and logged
  WITHOUT re-raising, because EventBridge retry on scheduled events
  is rarely what we want (a Tuesday afternoon glitch shouldn't
  flap the cron — recovery is via the admin trigger).
- For testing / backfill, the event may carry `{"week": N,
  "dry_run": true, "force": true, "seasons_back": 0}` — same
  shape the admin trigger accepts. `use_previous_season` is also
  accepted as a deprecated alias for `seasons_back=1`.
"""
from __future__ import annotations

from typing import Any

from lambdas.common.cron_settings import get_cron_setting
from lambdas.common.errors import ReportAlreadyExistsError, handle_errors
from lambdas.common.logger import get_logger
from lambdas.common.utility_helpers import success_response
from lambdas.common.weekly_orchestrator import run_weekly

log = get_logger(__file__)
HANDLER = "notif_ai_review_weekly"
LAMBDA_CRON_KEY = "notif_ai_review_weekly"


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log.info("Starting Weekly AI Review notification...")

    # Admin cron settings gate (admin-cron-settings).
    #   enabled=false  → no-op skip.
    #   test_mode=true → forward as dry_run=True to the orchestrator,
    #                    which reuses F3's existing admin-only delivery
    #                    path (no separate recipient filter needed).
    cron_setting = get_cron_setting(LAMBDA_CRON_KEY)
    if not cron_setting["enabled"]:
        log.info(
            f"{LAMBDA_CRON_KEY} disabled via admin_cron_settings — skipping"
        )
        return success_response(
            {"Success": True, "skipped": True, "reason": "disabled"},
            is_api=False,
        )
    cron_test_mode = bool(cron_setting["test_mode"])

    # EventBridge `scheduled` events don't carry a body — fields are
    # at the top level when present. Tests + admin trigger
    # backfills can pass overrides here.
    overrides = event if isinstance(event, dict) else {}
    week_override = _coerce_int(overrides.get("week"))
    dry_run = _coerce_bool(overrides.get("dry_run"), default=False)
    # When test_mode is enabled in admin_cron_settings, force dry_run on
    # — admin-only delivery is exactly what dry_run already does in F3.
    if cron_test_mode:
        if not dry_run:
            log.info(
                f"{LAMBDA_CRON_KEY} test_mode=true — forcing dry_run=True "
                f"(orchestrator will deliver to admin only)"
            )
        dry_run = True
    force = _coerce_bool(overrides.get("force"), default=False)
    seasons_back_raw = _coerce_int(overrides.get("seasons_back"))
    seasons_back = seasons_back_raw if seasons_back_raw is not None else 0
    # Back-compat alias — forwarded to the orchestrator which logs a
    # deprecation warning when truthy. Default False keeps the cron
    # call shape stable for existing snapshot tests.
    use_previous_season = _coerce_bool(
        overrides.get("use_previous_season"), default=False
    )

    try:
        result = run_weekly(
            week=week_override,
            dry_run=dry_run,
            force=force,
            seasons_back=seasons_back,
            use_previous_season=use_previous_season,
        )
    except ReportAlreadyExistsError as err:
        log.info(f"notif_ai_review_weekly: {err.message} — skipping")
        return success_response(
            {"Success": True, "skipped": True, "reason": "exists"},
            is_api=False,
        )
    except Exception as err:  # noqa: BLE001 — cron must not flap
        log.error(
            f"notif_ai_review_weekly: weekly run failed "
            f"({err.__class__.__name__}: {err})"
        )
        return success_response(
            {
                "Success": False,
                "Message": str(err),
                "error_type": err.__class__.__name__,
            },
            is_api=False,
            status_code=200,
        )

    log.info(
        f"notif_ai_review_weekly: status={result.get('status')} "
        f"week={result.get('week')} period={result.get('period')} "
        f"delivery={result.get('delivery_count')}"
    )

    return success_response(
        {
            "Success": True,
            "status": result.get("status"),
            "week": result.get("week"),
            "period": result.get("period"),
            "dry_run": result.get("dry_run"),
            "delivery_count": result.get("delivery_count"),
            "model": result.get("model"),
            "token_usage": result.get("token_usage"),
            "memory_count_in": result.get("memory_count_in"),
            "memory_count_out": result.get("memory_count_out"),
            "envelope_parsed": result.get("envelope_parsed"),
        },
        is_api=False,
    )


def _coerce_int(value: Any) -> int | None:
    """Cast event body integers (which may arrive as string from
    EventBridge `Input` payloads) to int. Returns None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Map various `bool | "true" | "false" | 1 | 0` shapes to a
    Python bool. Anything else falls back to `default`."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1", "yes"}:
            return True
        if value.strip().lower() in {"false", "0", "no"}:
            return False
    return default
