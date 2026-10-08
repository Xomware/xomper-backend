"""
Tests for the F3 weekly orchestrator + admin trigger handler.

Covers:
  - Orchestrator: idempotency (409 / force), pre-flight no-op
    outside the regular season, dry-run vs broadcast delivery,
    JSON envelope happy-path + parse-failure fallback, memory
    append, use_previous_season swap.
  - Admin handler: 403 / 409 / 200, week override, dry-run +
    force flag plumbing, use_previous_season flag pass-through.

External integrations (Sleeper, Anthropic, SES, SNS, Supabase,
DynamoDB ai_reports_store + ai_memories_store) are all
monkeypatched. Tests stay fast and deterministic.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest


ADMIN_ID = "594625531702460416"
LEAGUE_ID = "LEAGUE_ID"
PRIOR_LEAGUE_ID = "PRIOR_LEAGUE_ID"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_sleeper_users(count: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "user_id": f"u{i}",
            "display_name": f"Manager{i}",
            "metadata": {"team_name": f"Team{i}"},
        }
        for i in range(1, count + 1)
    ]


def _make_rosters(count: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "roster_id": i,
            "owner_id": f"u{i}",
            "settings": {},
            "players": [f"p{i}-{j}" for j in range(15)],
            "starters": [f"p{i}-{j}" for j in range(9)],
        }
        for i in range(1, count + 1)
    ]


def _make_matchups(count: int = 12) -> list[dict[str, Any]]:
    """Build a list of 6 head-to-head matchups across 12 rosters."""
    matchups: list[dict[str, Any]] = []
    for pair_idx in range(count // 2):
        mid = pair_idx + 1
        r1 = pair_idx * 2 + 1
        r2 = pair_idx * 2 + 2
        # alternate winners by parity
        a_pts = 130.0 - pair_idx
        b_pts = 90.0 + pair_idx
        matchups.append(
            {
                "roster_id": r1,
                "matchup_id": mid,
                "points": a_pts,
                "starters_points": [a_pts],
                "players_points": {f"p{r1}-0": a_pts, f"p{r1}-1": 5.0},
            }
        )
        matchups.append(
            {
                "roster_id": r2,
                "matchup_id": mid,
                "points": b_pts,
                "starters_points": [b_pts],
                "players_points": {f"p{r2}-0": b_pts, f"p{r2}-1": 8.0},
            }
        )
    return matchups


def _make_whitelisted_users(count: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "sleeper_user_id": ADMIN_ID if i == 1 else f"u{i}",
            "email": f"manager{i}@example.com",
            "display_name": f"Manager{i}",
            "sleeper_username": f"manager{i}",
            "is_active": True,
            "is_admin": i == 1,
            "id": f"row-{i}",
        }
        for i in range(1, count + 1)
    ]


def _ok_claude_envelope() -> str:
    """Return a JSON-envelope-shaped Claude response."""
    return json.dumps(
        {
            "body_markdown": (
                "# Week 4 Roast\n\n"
                "## Headline\n\n"
                "Manager1 dropped 130.0 — league high. Manager12 brought a "
                "sad 90.0 to the party.\n\n"
                "## Recaps\n\n"
                "Brutal margins all around.\n"
            ),
            "new_memories": [
                {
                    "text": "Manager1 went off for 130.0 — league high in Week 4.",
                    "manager_user_id": "u1",
                    "sentiment": "praise",
                },
                {
                    "text": "Manager12 mustered 90.0 in Week 4 — basement of the league.",
                    "manager_user_id": "u12",
                    "sentiment": "roast",
                },
                {
                    "text": "League average dropped to 110 in Week 4 — lowest of the year.",
                    "manager_user_id": None,
                    "sentiment": "lore",
                },
            ],
        }
    )


@pytest.fixture
def patched_orchestrator(monkeypatch: pytest.MonkeyPatch):
    """Patch every external dependency of
    `lambdas.common.weekly_orchestrator.run_weekly` and the memory
    store with deterministic stand-ins. Returns a state dict so
    tests can swap individual seams."""
    from lambdas.common import weekly_orchestrator as orch

    state: dict[str, Any] = {
        "writes": [],
        "metadata_updates": [],
        "memory_writes": [],
        "memory_list_calls": [],
        "emails_sent": [],
        "pushes_sent": [],
        "claude_calls": [],
        "claude_response": _ok_claude_envelope(),
        "claude_raises": None,
        "existing_report": None,
        "prior_memories": [],
        "nfl_state": {
            "season": "2026",
            "season_type": "regular",
            "week": 5,
        },
        "prior_league_id": PRIOR_LEAGUE_ID,
        "matchups_by_league_week": {},
        "whitelisted_users": _make_whitelisted_users(),
        "active_league": {
            "sleeper_league_id": LEAGUE_ID,
            "league_name": "CLT DYNASTY",
        },
    }

    monkeypatch.setattr(
        orch,
        "get_active_whitelisted_league",
        lambda: state["active_league"],
    )

    # Two-hop historical chain for `seasons_back` backfill tests:
    #   LEAGUE_ID (2026) -> PRIOR_LEAGUE_ID (2025) -> HIST_2024 (2024).
    league_chain: dict[str, dict[str, Any]] = {
        LEAGUE_ID: {
            "league_id": LEAGUE_ID,
            "name": "CLT DYNASTY",
            "previous_league_id": PRIOR_LEAGUE_ID,
            "season": "2026",
        },
        PRIOR_LEAGUE_ID: {
            "league_id": PRIOR_LEAGUE_ID,
            "name": "CLT DYNASTY (2025)",
            "previous_league_id": "HIST_2024",
            "season": "2025",
        },
        "HIST_2024": {
            "league_id": "HIST_2024",
            "name": "CLT DYNASTY (2024)",
            "previous_league_id": None,
            "season": "2024",
        },
    }
    state["league_chain"] = league_chain

    def _get_league(league_id: str) -> dict[str, Any]:
        if league_id in league_chain:
            entry = dict(league_chain[league_id])
            if league_id == LEAGUE_ID:
                # Honor the per-test override on the active league's
                # previous_league_id so the existing
                # `test_no_previous_league_raises` test keeps working.
                entry["previous_league_id"] = state["prior_league_id"]
            return entry
        return {
            "league_id": league_id,
            "name": "CLT DYNASTY",
            "previous_league_id": state["prior_league_id"],
            "season": "2026",
        }

    monkeypatch.setattr(orch, "get_sleeper_league", _get_league)
    monkeypatch.setattr(orch, "get_nfl_state", lambda: state["nfl_state"])

    state["matchup_calls"] = []

    def _matchups(league_id: str, week: int) -> list[dict[str, Any]]:
        state["matchup_calls"].append((league_id, week))
        # Allow tests to override matchups per (league, week).
        key = (league_id, week)
        if key in state["matchups_by_league_week"]:
            return state["matchups_by_league_week"][key]
        return _make_matchups()

    monkeypatch.setattr(orch, "get_sleeper_league_matchups", _matchups)

    monkeypatch.setattr(
        orch, "get_sleeper_league_users", lambda lid: _make_sleeper_users()
    )
    monkeypatch.setattr(
        orch, "get_sleeper_league_rosters", lambda lid: _make_rosters()
    )

    monkeypatch.setattr(
        orch, "get_previous_league_id", lambda lid: state["prior_league_id"]
    )

    monkeypatch.setattr(
        orch,
        "get_active_whitelisted_users",
        lambda: state["whitelisted_users"],
    )

    # ai_reports_store
    fake_reports = MagicMock()

    def _get_latest(league_id: str, report_type: str):
        return state["existing_report"]

    def _write_report(*, league_id, report_type, period, body_markdown, metadata):
        item = {
            "pk": f"LEAGUE#{league_id}",
            "sk": f"REPORT#{report_type}#{period}",
            "league_id": league_id,
            "report_type": report_type,
            "period": period,
            "body_markdown": body_markdown,
            "metadata": metadata,
            "created_at": "2026-09-10T18:00:00Z",
        }
        state["writes"].append(item)
        state["existing_report"] = item
        return item

    def _update_metadata(*, league_id, report_type, period, partial):
        state["metadata_updates"].append(
            {
                "league_id": league_id,
                "report_type": report_type,
                "period": period,
                "partial": partial,
            }
        )
        return {}

    def _get_report(*, league_id, report_type, period):
        override = state.get("fresh_metadata")
        if state["writes"]:
            base = state["writes"][-1]
            if override is None:
                return base
            return {**base, "metadata": {**(base.get("metadata") or {}), **override}}
        existing = state["existing_report"]
        return existing if existing and existing.get("period") == period else None

    def _stamp_broadcast_at(*, league_id, report_type, period):
        return _update_metadata(
            league_id=league_id,
            report_type=report_type,
            period=period,
            partial={"broadcast_at": "2026-09-10T18:30:00Z"},
        )

    fake_reports.get_latest = _get_latest
    fake_reports.get_report = _get_report
    fake_reports.write_report = _write_report
    fake_reports.update_metadata = _update_metadata
    fake_reports.stamp_broadcast_at = _stamp_broadcast_at
    monkeypatch.setattr(orch, "ai_reports_store", fake_reports)

    # ai_memories_store
    fake_memories = MagicMock()

    def _list_recent_memories(league_id: str, season, limit: int = 6):
        state["memory_list_calls"].append(
            {"league_id": league_id, "season": season, "limit": limit}
        )
        return list(state["prior_memories"][:limit])

    def _write_memory(*, league_id, season, week, text, sentiment, manager_user_id=None):
        item = {
            "league_id": league_id,
            "season": str(season),
            "week": int(week),
            "text": text,
            "sentiment": sentiment,
            "manager_user_id": manager_user_id,
        }
        state["memory_writes"].append(item)
        return item

    fake_memories.list_recent_memories = _list_recent_memories
    fake_memories.write_memory = _write_memory
    monkeypatch.setattr(orch, "ai_memories_store", fake_memories)

    # claude_helper
    def _generate(
        *,
        prompt: str,
        system: Any,
        model: str,
        max_tokens: int,
        return_usage: bool = False,
    ):
        if state["claude_raises"] is not None:
            raise state["claude_raises"]
        state["claude_calls"].append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "max_tokens": max_tokens,
                "return_usage": return_usage,
            }
        )
        usage = {
            "input_tokens": 5000,
            "output_tokens": 2800,
            "cache_read_input_tokens": 4500,
            "cache_creation_input_tokens": 500,
        }
        if return_usage:
            return state["claude_response"], usage
        return state["claude_response"]

    fake_claude = MagicMock()
    fake_claude.generate = _generate
    monkeypatch.setattr(orch, "claude_helper", fake_claude)

    # SES + SNS
    def _send_emails(tasks):
        state["emails_sent"].extend(tasks)
        return len(tasks), 0

    def _send_push(user_ids, title, body, category=None, data=None):
        state["pushes_sent"].append(
            {
                "user_ids": list(user_ids),
                "title": title,
                "body": body,
                "category": category,
                "data": data,
            }
        )
        return len(user_ids), 0

    monkeypatch.setattr(orch, "send_emails_concurrently", _send_emails)
    monkeypatch.setattr(orch, "send_push_to_users", _send_push)

    return state


# ---------------------------------------------------------------------------
# Orchestrator-layer tests
# ---------------------------------------------------------------------------


class TestOrchestratorWeekResolution:
    def test_week_override_honored(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=8, dry_run=True, force=False)
        assert result["week"] == 8
        assert result["period"] == "2026W08"

    def test_resolves_from_nfl_state_when_no_override(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "regular",
            "week": 7,
        }
        result = run_weekly(dry_run=True, force=False)
        assert result["week"] == 6  # nfl_state.week - 1
        assert result["period"] == "2026W06"

    def test_clamps_week_at_one(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "regular",
            "week": 1,
        }
        result = run_weekly(dry_run=True, force=False)
        assert result["week"] == 1

    def test_invalid_week_override_raises(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import ValidationError
        from lambdas.common.weekly_orchestrator import run_weekly

        with pytest.raises(ValidationError):
            run_weekly(week=0, dry_run=True, force=False)
        with pytest.raises(ValidationError):
            run_weekly(week=99, dry_run=True, force=False)


class TestOrchestratorPreFlight:
    def test_offseason_is_no_op(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "off",
            "week": 0,
        }
        result = run_weekly(dry_run=False, force=False)
        assert result["status"] == "skipped_offseason"
        assert result["skipped"] is True
        # No Claude call, no writes, no email.
        assert patched_orchestrator["claude_calls"] == []
        assert patched_orchestrator["writes"] == []
        assert patched_orchestrator["emails_sent"] == []

    def test_preseason_is_no_op(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "pre",
            "week": 0,
        }
        result = run_weekly(dry_run=False, force=False)
        assert result["status"] == "skipped_offseason"
        assert patched_orchestrator["claude_calls"] == []

    def test_post_season_is_allowed(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "post",
            "week": 19,
        }
        result = run_weekly(week=18, dry_run=True, force=False)
        assert result["status"] == "dry_run_sent"

    def test_use_previous_season_bypasses_preflight(
        self, patched_orchestrator
    ) -> None:
        """Dry-run calibration against 2025 data must work even
        when current NFL state is offseason."""
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "off",
            "week": 0,
        }
        result = run_weekly(
            week=4,
            dry_run=True,
            force=False,
            use_previous_season=True,
        )
        assert result["status"] == "dry_run_sent"
        # Claude was called.
        assert len(patched_orchestrator["claude_calls"]) == 1


class TestOrchestratorIdempotency:
    def test_existing_same_period_with_force_false_raises(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import ReportAlreadyExistsError
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2026W04",
            "created_at": "2026-09-10T18:00:00Z",
        }
        with pytest.raises(ReportAlreadyExistsError):
            run_weekly(week=4, dry_run=True, force=False)

    def test_existing_different_period_does_not_raise(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        # Existing row is for an EARLIER week — the new week should
        # still write.
        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2026W02",
            "created_at": "2026-08-28T18:00:00Z",
        }
        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["period"] == "2026W04"
        assert len(patched_orchestrator["writes"]) == 1

    def test_force_true_overwrites(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2026W04",
            "created_at": "2026-09-10T18:00:00Z",
        }
        result = run_weekly(week=4, dry_run=True, force=True)
        assert result["status"] == "dry_run_sent"
        assert len(patched_orchestrator["writes"]) == 1


class TestOrchestratorDelivery:
    def test_dry_run_delivers_to_admin_only(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)

        assert result["delivery_count"] == 1
        assert len(patched_orchestrator["emails_sent"]) == 1
        recipient, subject, _h, _t = patched_orchestrator["emails_sent"][0]
        assert recipient == "manager1@example.com"
        assert subject.startswith("[DRY RUN]")
        # Push: only the admin sleeper_user_id.
        push = patched_orchestrator["pushes_sent"][0]
        assert push["user_ids"] == [ADMIN_ID]
        assert push["title"].startswith("[DRY RUN]")
        # Dry-run path does NOT stamp broadcast_at.
        partials = [u["partial"] for u in patched_orchestrator["metadata_updates"]]
        assert not any("broadcast_at" in p for p in partials)

    def test_broadcast_delivers_to_all_twelve(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=False, force=True)

        assert result["delivery_count"] == 12
        assert len(patched_orchestrator["emails_sent"]) == 12
        push = patched_orchestrator["pushes_sent"][0]
        assert len(push["user_ids"]) == 12
        assert push["title"] == "This week's AI recap is in"
        partials = [u["partial"] for u in patched_orchestrator["metadata_updates"]]
        assert any("broadcast_at" in p for p in partials)


    def test_delivery_off_stores_without_email_or_push(
        self, patched_orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lambdas.common import weekly_orchestrator as orch

        monkeypatch.setattr(orch, "AI_REVIEW_WEEKLY_DELIVER", False)
        result = orch.run_weekly(week=4, dry_run=False, force=False)

        assert result["status"] == "stored"
        assert result["delivery_count"] == 0
        assert len(patched_orchestrator["writes"]) == 1
        assert patched_orchestrator["emails_sent"] == []
        assert patched_orchestrator["pushes_sent"] == []
        partials = [u["partial"] for u in patched_orchestrator["metadata_updates"]]
        assert not any("broadcast_at" in p for p in partials)


class TestOrchestratorRenewedLeague:
    def test_matchups_from_renewal_stored_under_whitelisted_id(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.constants import CLT_LEAGUE_ID
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["active_league"] = {"sleeper_league_id": "CLT_2025"}
        patched_orchestrator["league_chain"][CLT_LEAGUE_ID] = {
            "league_id": CLT_LEAGUE_ID,
            "name": "Charlotte Dynasty League",
            "previous_league_id": "CLT_2025",
            "season": "2026",
        }

        result = run_weekly(week=4, dry_run=True, force=False)

        assert patched_orchestrator["matchup_calls"] == [(CLT_LEAGUE_ID, 4)]
        write = patched_orchestrator["writes"][0]
        assert write["pk"] == "LEAGUE#CLT_2025"
        assert result["period"] == "2026W04"
        assert write["metadata"]["matchup_league_id"] == CLT_LEAGUE_ID

    def test_unrelated_whitelisted_league_keeps_its_own_matchups(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)

        assert patched_orchestrator["matchup_calls"] == [(LEAGUE_ID, 4)]


class TestOrchestratorMemories:
    def test_writes_memories_on_happy_path(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)

        # _ok_claude_envelope returns 3 memories.
        assert result["memory_count_out"] == 3
        assert len(patched_orchestrator["memory_writes"]) == 3
        for mw in patched_orchestrator["memory_writes"]:
            assert mw["league_id"] == LEAGUE_ID
            assert mw["season"] == "2026"
            assert mw["week"] == 4
            assert mw["sentiment"] in {"roast", "praise", "lore"}

    def test_uses_prior_memories_as_context(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["prior_memories"] = [
            {
                "memory_id": "m1",
                "season": "2026",
                "week": 3,
                "text": "preexisting memory",
                "sentiment": "lore",
                "manager_user_id": "u3",
            }
        ]
        result = run_weekly(week=4, dry_run=True, force=False)

        assert result["memory_count_in"] == 1
        # Prior-memory text should appear in the user prompt.
        call = patched_orchestrator["claude_calls"][0]
        assert "preexisting memory" in call["prompt"]

    def test_memory_lookback_calls_store_with_default_limit(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        calls = patched_orchestrator["memory_list_calls"]
        assert len(calls) == 1
        assert calls[0]["limit"] == 6
        assert calls[0]["league_id"] == LEAGUE_ID

    def test_invalid_sentiment_dropped_from_memories(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["claude_response"] = json.dumps(
            {
                "body_markdown": "# body",
                "new_memories": [
                    {
                        "text": "keep me",
                        "manager_user_id": "u1",
                        "sentiment": "roast",
                    },
                    {
                        "text": "drop me",
                        "manager_user_id": "u2",
                        "sentiment": "snark",  # invalid
                    },
                ],
            }
        )

        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["memory_count_out"] == 1
        assert (
            patched_orchestrator["memory_writes"][0]["text"] == "keep me"
        )

    def test_memories_capped_at_five(self, patched_orchestrator) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        memories = [
            {"text": f"mem{i}", "manager_user_id": "u1", "sentiment": "lore"}
            for i in range(8)
        ]
        patched_orchestrator["claude_response"] = json.dumps(
            {"body_markdown": "# body", "new_memories": memories}
        )
        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["memory_count_out"] == 5


class TestOrchestratorEnvelopeParse:
    def test_clean_envelope_marked_parsed(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["envelope_parsed"] is True

    def test_non_json_response_persists_as_raw_markdown(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["claude_response"] = (
            "# Week 4 (no JSON!)\n\nSomething went sideways."
        )
        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["envelope_parsed"] is False
        # Report persisted with the raw text.
        write = patched_orchestrator["writes"][0]
        assert "no JSON" in write["body_markdown"]
        # No memories appended.
        assert result["memory_count_out"] == 0
        assert patched_orchestrator["memory_writes"] == []

    def test_envelope_missing_body_falls_back(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["claude_response"] = json.dumps(
            {"new_memories": []}
        )
        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["envelope_parsed"] is False

    def test_envelope_with_code_fences_still_parses(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        # Some Claude drift wraps the JSON in ```json fences.
        envelope = json.dumps(
            {
                "body_markdown": "# Week 4\n\nbody",
                "new_memories": [
                    {
                        "text": "fenced memory",
                        "manager_user_id": "u1",
                        "sentiment": "roast",
                    }
                ],
            }
        )
        patched_orchestrator["claude_response"] = (
            f"```json\n{envelope}\n```"
        )
        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["envelope_parsed"] is True
        assert result["memory_count_out"] == 1


class TestOrchestratorMetadata:
    def test_metadata_carries_full_provenance(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(
            week=4,
            dry_run=True,
            force=False,
            created_by_user_id="caller-x",
        )
        write = patched_orchestrator["writes"][0]
        meta = write["metadata"]
        assert meta["model"] == "claude-haiku-4-5"
        assert meta["prompt_version"].startswith("f3-weekly-")
        assert meta["dry_run"] is True
        assert meta["week"] == 4
        assert meta["nfl_season"] == "2026"
        assert meta["nfl_season_type"] == "regular"
        assert meta["created_by_user_id"] == "caller-x"
        assert meta["token_usage"]["output_tokens"] == 2800
        assert meta["envelope_parsed"] is True

    def test_write_uses_weekly_period_format(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        write = patched_orchestrator["writes"][0]
        assert write["report_type"] == "weekly"
        assert write["period"] == "2026W04"


class TestOrchestratorPreviousSeason:
    def test_swaps_to_previous_league_id(
        self, patched_orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lambdas.common import weekly_orchestrator as orch

        called_with: list[tuple[str, int]] = []
        orig_matchups = orch.get_sleeper_league_matchups

        def _trace_matchups(league_id: str, week: int):
            called_with.append((league_id, week))
            return orig_matchups(league_id, week)

        monkeypatch.setattr(
            orch, "get_sleeper_league_matchups", _trace_matchups
        )

        orch.run_weekly(
            week=4,
            dry_run=True,
            force=False,
            use_previous_season=True,
        )
        # Matchups fetched from PRIOR_LEAGUE_ID.
        assert called_with[0][0] == PRIOR_LEAGUE_ID

    def test_no_previous_league_raises(self, patched_orchestrator) -> None:
        from lambdas.common.errors import NotFoundError
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["prior_league_id"] = None
        with pytest.raises(NotFoundError):
            run_weekly(
                week=4,
                dry_run=True,
                force=False,
                use_previous_season=True,
            )


class TestOrchestratorClaudeFailure:
    def test_claude_failure_does_not_write_report(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import ClaudeAPIError
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["claude_raises"] = ClaudeAPIError(
            message="Anthropic 503", model="claude-haiku-4-5"
        )
        with pytest.raises(ClaudeAPIError):
            run_weekly(week=4, dry_run=True, force=False)
        assert patched_orchestrator["writes"] == []
        assert patched_orchestrator["memory_writes"] == []
        assert patched_orchestrator["emails_sent"] == []


# ---------------------------------------------------------------------------
# Handler-layer tests
# ---------------------------------------------------------------------------


def _api_event(
    *,
    sleeper_user_id: str | None = ADMIN_ID,
    body: dict | None = None,
) -> dict:
    headers = (
        {"X-Sleeper-User-Id": sleeper_user_id} if sleeper_user_id else {}
    )
    return {
        "httpMethod": "POST",
        "path": "/admin/ai-review-weekly-trigger",
        "headers": headers,
        "body": json.dumps(body or {}),
    }


class TestHandler:
    def test_403_when_not_admin(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h
        from lambdas.common.admin_gate import NotAdmin

        def _raise(event, body):
            raise NotAdmin("not authorized")

        monkeypatch.setattr(h, "require_admin", _raise)

        response = h.handler(
            _api_event(body={"dry_run": True}),
            context=None,
        )
        assert response["statusCode"] == 403

    def test_409_on_existing_same_period(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )
        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2026W04",
            "created_at": "2026-09-10T18:00:00Z",
        }

        response = h.handler(
            _api_event(body={"week": 4, "dry_run": True, "force": False}),
            context=None,
        )
        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"] == "already_generated"
        assert body["existing"]["period"] == "2026W04"

    def test_200_dry_run_with_explicit_week(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )

        response = h.handler(
            _api_event(body={"week": 5, "dry_run": True}),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["Success"] is True
        assert body["dry_run"] is True
        assert body["week"] == 5
        assert body["period"] == "2026W05"
        assert body["delivery_count"] == 1

    def test_200_broadcast_all_twelve(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )

        response = h.handler(
            _api_event(
                body={"week": 4, "dry_run": False, "force": True}
            ),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dry_run"] is False
        assert body["delivery_count"] == 12

    def test_defaults_dry_run_true_no_week_override(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )

        response = h.handler(_api_event(body={}), context=None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["dry_run"] is True
        # nfl_state.week=5 → recap week 4.
        assert body["week"] == 4

    def test_use_previous_season_flag_passed_through(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )

        response = h.handler(
            _api_event(
                body={
                    "week": 4,
                    "dry_run": True,
                    "force": True,
                    "use_previous_season": True,
                }
            ),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["use_previous_season"] is True


# ---------------------------------------------------------------------------
# Backfill — seasons_back (replaces use_previous_season)
# ---------------------------------------------------------------------------


class TestOrchestratorSeasonsBack:
    """Covers the generic `seasons_back` arg: 0 = current,
    1 = prior season, 2 = two seasons ago, etc. The deprecated
    `use_previous_season` alias still maps to 1 for back-compat."""

    def test_seasons_back_zero_is_default_current_season(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)
        assert result["seasons_back"] == 0
        assert result["use_previous_season"] is False
        # Matchups fetched against the active league.
        assert patched_orchestrator["matchup_calls"][0][0] == LEAGUE_ID
        # Period uses the current season.
        assert result["period"] == "2026W04"

    def test_seasons_back_one_walks_to_prior_league(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(
            week=4, dry_run=True, force=False, seasons_back=1
        )
        assert result["seasons_back"] == 1
        # Back-compat surface still set.
        assert result["use_previous_season"] is True
        # Matchups fetched against the prior league.
        assert patched_orchestrator["matchup_calls"][0][0] == PRIOR_LEAGUE_ID
        # Period uses the historical season (2025).
        assert result["period"] == "2025W04"
        write = patched_orchestrator["writes"][0]
        assert write["period"] == "2025W04"
        assert write["metadata"]["seasons_back"] == 1
        assert write["metadata"]["matchup_league_id"] == PRIOR_LEAGUE_ID

    def test_seasons_back_two_walks_twice(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(
            week=4, dry_run=True, force=False, seasons_back=2
        )
        assert result["seasons_back"] == 2
        # Matchups fetched against the two-hop league.
        assert patched_orchestrator["matchup_calls"][0][0] == "HIST_2024"
        assert result["period"] == "2024W04"
        write = patched_orchestrator["writes"][0]
        assert write["period"] == "2024W04"
        assert write["metadata"]["matchup_league_id"] == "HIST_2024"

    def test_seasons_back_exceeds_chain_raises(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import NotFoundError
        from lambdas.common.weekly_orchestrator import run_weekly

        # Chain only goes 2 hops deep, request 10.
        with pytest.raises(NotFoundError):
            run_weekly(
                week=4, dry_run=True, force=False, seasons_back=10
            )
        # No write happened.
        assert patched_orchestrator["writes"] == []

    def test_seasons_back_negative_raises_validation(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import ValidationError
        from lambdas.common.weekly_orchestrator import run_weekly

        with pytest.raises(ValidationError):
            run_weekly(
                week=4, dry_run=True, force=False, seasons_back=-1
            )

    def test_use_previous_season_back_compat_maps_to_one(
        self, patched_orchestrator
    ) -> None:
        """Deprecated alias still works — should resolve to
        seasons_back=1 with the same downstream behavior."""
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(
            week=4,
            dry_run=True,
            force=False,
            use_previous_season=True,
        )
        assert result["seasons_back"] == 1
        assert result["use_previous_season"] is True
        assert patched_orchestrator["matchup_calls"][0][0] == PRIOR_LEAGUE_ID
        assert result["period"] == "2025W04"

    def test_seasons_back_bypasses_offseason_preflight(
        self, patched_orchestrator
    ) -> None:
        """Backfill against finished seasons works even when current
        NFL state is offseason — this is the whole point."""
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "off",
            "week": 0,
        }
        result = run_weekly(
            week=4, dry_run=True, force=False, seasons_back=2
        )
        assert result["status"] == "dry_run_sent"
        assert result["period"] == "2024W04"

    def test_idempotency_per_historical_period(
        self, patched_orchestrator
    ) -> None:
        """Existing 2024W04 row should not block 2025W04 writes."""
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2024W04",
            "created_at": "2025-01-01T00:00:00Z",
        }
        result = run_weekly(
            week=4, dry_run=True, force=False, seasons_back=1
        )
        assert result["period"] == "2025W04"

    def test_idempotency_same_historical_period_409s(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.errors import ReportAlreadyExistsError
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["existing_report"] = {
            "league_id": LEAGUE_ID,
            "report_type": "weekly",
            "period": "2024W04",
            "created_at": "2025-01-01T00:00:00Z",
        }
        with pytest.raises(ReportAlreadyExistsError):
            run_weekly(
                week=4, dry_run=True, force=False, seasons_back=2
            )


class TestHandlerSeasonsBack:
    def test_handler_seasons_back_2_returns_2024_period(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )
        response = h.handler(
            _api_event(
                body={
                    "week": 4,
                    "dry_run": True,
                    "force": True,
                    "seasons_back": 2,
                }
            ),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["seasons_back"] == 2
        assert body["period"] == "2024W04"

    def test_handler_seasons_back_string_coerced(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )
        response = h.handler(
            _api_event(
                body={"week": 4, "dry_run": True, "seasons_back": "1"}
            ),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["seasons_back"] == 1
        assert body["period"] == "2025W04"

    def test_handler_seasons_back_10_returns_404(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )
        response = h.handler(
            _api_event(
                body={"week": 4, "dry_run": True, "seasons_back": 10}
            ),
            context=None,
        )
        assert response["statusCode"] == 404

    def test_handler_iOS_payload_unchanged_no_seasons_back(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """iOS only sends {week, dry_run, force} — seasons_back
        defaults to 0 and the response shape doesn't change."""
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h,
            "require_admin",
            lambda event, body: {"sleeper_user_id": ADMIN_ID},
        )
        response = h.handler(
            _api_event(body={"week": 4, "dry_run": True, "force": False}),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["seasons_back"] == 0


# ---------------------------------------------------------------------------
# Admin Portal F2 — email previews on dry-run responses
# ---------------------------------------------------------------------------


class TestOrchestratorPreviewsDryRun:
    """Locks the dry-run preview surface added in Admin Portal F2."""

    def test_dry_run_returns_one_preview_per_active_user(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)

        previews = result["previews"]
        assert isinstance(previews, list)
        assert len(previews) == 12

    def test_each_preview_has_required_fields(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)

        required = {
            "recipient_user_id",
            "recipient_email",
            "display_name",
            "subject",
            "text_body",
            "html_body_excerpt",
        }
        for preview in result["previews"]:
            assert required.issubset(preview.keys())
            assert "html_body" not in preview

    def test_previews_sorted_alphabetically_by_display_name(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["whitelisted_users"] = [
            {
                "sleeper_user_id": ADMIN_ID,
                "email": "zach@example.com",
                "display_name": "Zach",
                "sleeper_username": "zach",
                "is_active": True,
                "is_admin": True,
                "id": "row-1",
            },
            {
                "sleeper_user_id": "u2",
                "email": "adam@example.com",
                "display_name": "Adam",
                "sleeper_username": "adam",
                "is_active": True,
                "is_admin": False,
                "id": "row-2",
            },
            {
                "sleeper_user_id": "u3",
                "email": "mike@example.com",
                "display_name": "Mike",
                "sleeper_username": "mike",
                "is_active": True,
                "is_admin": False,
                "id": "row-3",
            },
            {
                "sleeper_user_id": "u4",
                "email": "beth@example.com",
                "display_name": "Beth",
                "sleeper_username": "beth",
                "is_active": True,
                "is_admin": False,
                "id": "row-4",
            },
        ]

        result = run_weekly(week=4, dry_run=True, force=False)
        names = [p["display_name"] for p in result["previews"]]
        assert names == ["Adam", "Beth", "Mike", "Zach"]

    def test_text_body_capped_at_4096_chars(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Weekly recap body that exceeds 4KB still caps cleanly."""
        from lambdas.common import weekly_orchestrator as orch

        # JSON-envelope-wrapping: emit a body_markdown that is itself
        # large enough to exceed 4096 chars on the plain-text path.
        big_body = "X" * 10_000
        envelope = json.dumps(
            {
                "body_markdown": big_body,
                "new_memories": [],
            }
        )
        patched_orchestrator["claude_response"] = envelope

        def _generate(
            *, prompt, system, model, max_tokens, return_usage=False
        ):
            usage = {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
            if return_usage:
                return envelope, usage
            return envelope

        fake_claude = MagicMock()
        fake_claude.generate = _generate
        monkeypatch.setattr(orch, "claude_helper", fake_claude)

        result = orch.run_weekly(week=4, dry_run=True, force=False)
        for preview in result["previews"]:
            assert len(preview["text_body"]) <= 4096

    def test_html_body_excerpt_capped_at_500_chars(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=True, force=False)
        for preview in result["previews"]:
            assert len(preview["html_body_excerpt"]) <= 500

    def test_preview_subject_contains_week_label(
        self, patched_orchestrator
    ) -> None:
        """Weekly previews must reflect the resolved week in their
        subjects (regression guard for the week-threading helper)."""
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=11, dry_run=True, force=False)
        for preview in result["previews"]:
            assert "Week 11" in preview["subject"]


class TestOrchestratorPreviewsBroadcast:
    """Broadcast (dry_run=False) responses must NOT include previews."""

    def test_broadcast_returns_none_previews(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        result = run_weekly(week=4, dry_run=False, force=True)

        assert result["previews"] is None

    def test_offseason_skip_returns_none_previews(
        self, patched_orchestrator
    ) -> None:
        """No-op short-circuit still serializes a `previews` key for
        wire-shape stability."""
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["nfl_state"] = {
            "season": "2026",
            "season_type": "off",
            "week": 0,
        }
        result = run_weekly(dry_run=True, force=False)
        assert result["status"] == "skipped_offseason"
        assert result["previews"] is None


class TestHandlerPreviews:
    """End-to-end through the lambda handler."""

    def test_handler_dry_run_response_includes_previews(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h, "require_admin", lambda event, body: {"sleeper_user_id": ADMIN_ID}
        )
        response = h.handler(
            _api_event(body={"week": 4, "dry_run": True}),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert isinstance(body["previews"], list)
        assert len(body["previews"]) == 12

    def test_handler_broadcast_response_previews_null(
        self,
        patched_orchestrator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from lambdas.api_admin_ai_review_weekly_trigger import handler as h

        monkeypatch.setattr(
            h, "require_admin", lambda event, body: {"sleeper_user_id": ADMIN_ID}
        )
        response = h.handler(
            _api_event(body={"week": 4, "dry_run": False, "force": True}),
            context=None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["previews"] is None
        assert body["period"] == "2026W04"


# ---------------------------------------------------------------------------
# Per-matchup blurbs (iOS `MatchupBlurbCardView` surface)
# ---------------------------------------------------------------------------
#
# The iOS `MatchupsView` decodes `metadata.matchups[]` per weekly
# report and renders a per-matchup blurb under each card. The
# historical 36-report backfill stamped the array inline; these
# tests lock the live orchestrator path so future cron fires emit
# the same shape.


class TestRenderMatchupBlurb:
    """Locks the deterministic margin-banded template renderer."""

    def _base(self) -> dict[str, Any]:
        return {
            "matchup_id": 1,
            "team_a": "Brock Party",
            "team_b": "Gangsters of Love",
            "handle_a": "domgiordano",
            "handle_b": "ozz",
            "user_id_a": "uA",
            "user_id_b": "uB",
        }

    def test_obliterated_band_ge_60(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "204.9",
            "score_b": "140.9",
            "margin": "64.0",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "obliterated" in blurb
        assert "shellacking" in blurb
        assert "Brock Party" in blurb
        assert "204.9" in blurb
        assert "140.9" in blurb

    def test_blew_out_band_ge_40(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "180.0",
            "score_b": "135.0",
            "margin": "45.0",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "blew out" in blurb
        assert "no nail-biting" in blurb

    def test_handled_band_ge_20(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "150.0",
            "score_b": "125.0",
            "margin": "25.0",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "handled" in blurb
        assert "comfortable" in blurb

    def test_edged_band_ge_8(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "132.3",
            "score_b": "124.3",
            "margin": "8.0",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "edged" in blurb
        assert "MNF" in blurb

    def test_squeaked_band_ge_3(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "120.0",
            "score_b": "115.0",
            "margin": "5.0",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "squeaked past" in blurb
        assert "grinder" in blurb

    def test_stole_one_band_lt_3(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "100.5",
            "score_b": "99.0",
            "margin": "1.5",
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "stole one" in blurb
        assert "heartbreaker" in blurb
        # Loser handle gets called out by name in the tightest band.
        assert "ozz" in blurb

    def test_tie_template(self) -> None:
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "110.0",
            "score_b": "110.0",
            "margin": "0.0",
            "winner": "tie",
        }
        blurb = render_matchup_blurb(m)
        assert "TIED" in blurb
        assert "Brock Party" in blurb
        assert "Gangsters of Love" in blurb
        assert "stuck the landing" in blurb

    def test_winner_b_flips_team_order(self) -> None:
        """When winner='b', the template names team_b as the winner."""
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": "100.0",
            "score_b": "150.0",
            "margin": "50.0",
            "winner": "b",
        }
        blurb = render_matchup_blurb(m)
        assert "blew out" in blurb
        # team_b name comes first as the winner.
        assert blurb.index("Gangsters of Love") < blurb.index("Brock Party")

    def test_accepts_numeric_scores(self) -> None:
        """Scores as floats (not strings) still render cleanly —
        defensive against shape drift."""
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        m = {
            **self._base(),
            "score_a": 145.7,
            "score_b": 95.3,
            "margin": 50.4,
            "winner": "a",
        }
        blurb = render_matchup_blurb(m)
        assert "blew out" in blurb
        assert "145.7" in blurb
        assert "95.3" in blurb

    def test_band_boundaries_inclusive(self) -> None:
        """Exact-threshold margins land in the higher band."""
        from lambdas.common.weekly_orchestrator import render_matchup_blurb

        # Exactly 60.0 -> obliterated band.
        m60 = {
            **self._base(),
            "score_a": "200.0",
            "score_b": "140.0",
            "margin": "60.0",
            "winner": "a",
        }
        assert "obliterated" in render_matchup_blurb(m60)

        # Exactly 40.0 -> blew out band.
        m40 = {
            **self._base(),
            "score_a": "175.0",
            "score_b": "135.0",
            "margin": "40.0",
            "winner": "a",
        }
        assert "blew out" in render_matchup_blurb(m40)

        # Exactly 20.0 -> handled band.
        m20 = {
            **self._base(),
            "score_a": "150.0",
            "score_b": "130.0",
            "margin": "20.0",
            "winner": "a",
        }
        assert "handled" in render_matchup_blurb(m20)

        # Exactly 8.0 -> edged band.
        m8 = {
            **self._base(),
            "score_a": "120.0",
            "score_b": "112.0",
            "margin": "8.0",
            "winner": "a",
        }
        assert "edged" in render_matchup_blurb(m8)

        # Exactly 3.0 -> squeaked band.
        m3 = {
            **self._base(),
            "score_a": "110.0",
            "score_b": "107.0",
            "margin": "3.0",
            "winner": "a",
        }
        assert "squeaked past" in render_matchup_blurb(m3)


class TestOrchestratorMatchupBlurbsInMetadata:
    """End-to-end through `run_weekly`: metadata.matchups must be
    present, length-correct, and shaped for the iOS decoder."""

    def test_metadata_matchups_present_and_length_matches(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        write = patched_orchestrator["writes"][0]
        meta = write["metadata"]

        assert "matchups" in meta
        # _make_matchups() builds 6 head-to-heads across 12 rosters.
        assert isinstance(meta["matchups"], list)
        assert len(meta["matchups"]) == 6

    def test_each_blurb_has_wire_shape_required_keys(
        self, patched_orchestrator
    ) -> None:
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]

        required = {
            "matchup_id",
            "team_a",
            "team_b",
            "handle_a",
            "handle_b",
            "user_id_a",
            "user_id_b",
            "score_a",
            "score_b",
            "margin",
            "winner",
            "blurb",
        }
        for entry in meta["matchups"]:
            assert required.issubset(entry.keys())
            # boto3-safe: scores + margin must be strings.
            assert isinstance(entry["score_a"], str)
            assert isinstance(entry["score_b"], str)
            assert isinstance(entry["margin"], str)
            assert entry["winner"] in {"a", "b", "tie"}
            assert isinstance(entry["blurb"], str) and entry["blurb"]

    def test_empty_matchups_yields_empty_blurb_list(
        self, patched_orchestrator
    ) -> None:
        """No matchups for the week (off-week / Sleeper hiccup) =>
        empty list. Must not crash; must not omit the key."""
        from lambdas.common.weekly_orchestrator import run_weekly

        patched_orchestrator["matchups_by_league_week"][(LEAGUE_ID, 4)] = []
        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]
        assert meta["matchups"] == []

    def test_blurb_strings_are_markdown_one_liners(
        self, patched_orchestrator
    ) -> None:
        """Every emitted blurb opens with a bold markdown segment —
        matches the backfilled historical voice."""
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]
        for entry in meta["matchups"]:
            assert entry["blurb"].startswith("**")

    def test_winner_axis_consistent_with_scores(
        self, patched_orchestrator
    ) -> None:
        """The `winner` enum must agree with the score_a/score_b
        comparison. Decoder uses `winner` to drive a/b badges; any
        mismatch leaks into the UI."""
        from lambdas.common.weekly_orchestrator import run_weekly

        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]
        for entry in meta["matchups"]:
            a = float(entry["score_a"])
            b = float(entry["score_b"])
            if abs(a - b) < 0.0001:
                assert entry["winner"] == "tie"
            elif a > b:
                assert entry["winner"] == "a"
            else:
                assert entry["winner"] == "b"

    def test_unpaired_matchups_skipped(
        self, patched_orchestrator
    ) -> None:
        """Solo matchup entries (bye-week placeholder from Sleeper)
        must not appear in the blurb list — same rule as the prompt
        builder."""
        from lambdas.common.weekly_orchestrator import run_weekly

        # Build 5 normal pairs + 1 lone entry.
        normal = _make_matchups(count=10)
        normal.append(
            {
                "roster_id": 11,
                "matchup_id": 99,
                "points": 100.0,
                "starters_points": [100.0],
                "players_points": {"p11-0": 100.0},
            }
        )
        patched_orchestrator["matchups_by_league_week"][(LEAGUE_ID, 4)] = normal
        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]
        # 5 pairs => 5 blurbs; the solo entry is dropped.
        assert len(meta["matchups"]) == 5

    def test_blurb_count_equals_matchup_pair_count(
        self, patched_orchestrator
    ) -> None:
        """For arbitrary pair counts the emitted blurb list length
        equals the number of fully-paired matchups."""
        from lambdas.common.weekly_orchestrator import run_weekly

        # 4 pairs across 8 rosters.
        patched_orchestrator["matchups_by_league_week"][(LEAGUE_ID, 4)] = (
            _make_matchups(count=8)
        )
        run_weekly(week=4, dry_run=True, force=False)
        meta = patched_orchestrator["writes"][0]["metadata"]
        assert len(meta["matchups"]) == 4
