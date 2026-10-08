"""
Tests for `lambdas.notif_ai_review_weekly.handler`.

The handler is a thin EventBridge entry that delegates to
`weekly_orchestrator.run_weekly`. Verifies:
  - Default cron fire (no body) flows broadcast through the
    orchestrator with current-week resolution.
  - Event overrides (`week`, `dry_run`, `force`,
    `use_previous_season`) plumb through.
  - Orchestrator exceptions are caught and surfaced as a
    `Success: False` response WITHOUT raising — EventBridge retry
    on scheduled events is not what we want.
  - Off-season returns `skipped_offseason` cleanly.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patched_handler(monkeypatch: pytest.MonkeyPatch):
    """Stub the orchestrator so we can assert on call shape."""
    from lambdas.notif_ai_review_weekly import handler as h

    state: dict[str, Any] = {
        "calls": [],
        "response": {
            "status": "broadcast",
            "report_id": "REPORT#weekly#2026W04",
            "report": {"sk": "REPORT#weekly#2026W04"},
            "dry_run": False,
            "delivery_count": 12,
            "model": "claude-haiku-4-5",
            "token_usage": {"input_tokens": 5000, "output_tokens": 2800},
            "week": 4,
            "period": "2026W04",
            "memory_count_in": 3,
            "memory_count_out": 4,
            "envelope_parsed": True,
        },
        "raises": None,
    }

    def _run_weekly(**kwargs):
        state["calls"].append(kwargs)
        if state["raises"] is not None:
            raise state["raises"]
        return state["response"]

    monkeypatch.setattr(h, "run_weekly", _run_weekly)
    return state


def test_default_event_calls_orchestrator_with_broadcast_defaults(
    patched_handler,
) -> None:
    from lambdas.notif_ai_review_weekly.handler import handler

    response = handler({}, context=None)

    assert response["statusCode"] == 200
    assert len(patched_handler["calls"]) == 1
    call = patched_handler["calls"][0]
    assert call["dry_run"] is False
    assert call["force"] is False
    assert call["use_previous_season"] is False
    assert call["week"] is None


def test_event_override_week_passes_through(patched_handler) -> None:
    from lambdas.notif_ai_review_weekly.handler import handler

    handler({"week": 5}, context=None)
    call = patched_handler["calls"][0]
    assert call["week"] == 5


def test_event_string_week_coerced_to_int(patched_handler) -> None:
    from lambdas.notif_ai_review_weekly.handler import handler

    handler({"week": "7"}, context=None)
    call = patched_handler["calls"][0]
    assert call["week"] == 7


def test_event_dry_run_and_force_flags_pass_through(patched_handler) -> None:
    from lambdas.notif_ai_review_weekly.handler import handler

    handler(
        {
            "week": 4,
            "dry_run": True,
            "force": True,
            "use_previous_season": True,
        },
        context=None,
    )
    call = patched_handler["calls"][0]
    assert call["dry_run"] is True
    assert call["force"] is True
    assert call["use_previous_season"] is True


def test_orchestrator_exception_does_not_raise(patched_handler) -> None:
    from lambdas.common.errors import ClaudeAPIError
    from lambdas.notif_ai_review_weekly.handler import handler

    patched_handler["raises"] = ClaudeAPIError(
        message="Anthropic 503", model="claude-haiku-4-5"
    )
    response = handler({}, context=None)
    # Handler MUST swallow + log so EventBridge doesn't retry.
    assert response["statusCode"] == 200
    body = response["body"]
    if isinstance(body, str):
        import json
        body = json.loads(body)
    assert body["Success"] is False
    assert "Anthropic 503" in body["Message"]


def test_skipped_offseason_response(patched_handler) -> None:
    from lambdas.notif_ai_review_weekly.handler import handler

    patched_handler["response"] = {
        "status": "skipped_offseason",
        "report": None,
        "dry_run": False,
        "delivery_count": 0,
        "model": "claude-haiku-4-5",
        "token_usage": None,
        "week": 1,
        "period": "2026W01",
        "memory_count_in": 0,
        "memory_count_out": 0,
        "envelope_parsed": None,
    }

    response = handler({}, context=None)
    assert response["statusCode"] == 200
    body = response["body"]
    if isinstance(body, str):
        import json
        body = json.loads(body)
    assert body["status"] == "skipped_offseason"
    assert body["delivery_count"] == 0


def test_existing_period_is_a_clean_skip(patched_handler) -> None:
    import json

    from lambdas.common.errors import ReportAlreadyExistsError
    from lambdas.notif_ai_review_weekly.handler import handler

    patched_handler["raises"] = ReportAlreadyExistsError(
        message="A weekly report already exists for period 2026W04"
    )
    response = handler({}, context=None)
    body = response["body"]
    if isinstance(body, str):
        body = json.loads(body)
    assert body == {"Success": True, "skipped": True, "reason": "exists"}
