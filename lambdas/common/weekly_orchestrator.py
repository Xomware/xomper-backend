"""
Weekly AI Review orchestrator (F3)
==================================
Pure orchestration logic for F3. Lives in `lambdas/common/` because
both `notif_ai_review_weekly` (EventBridge cron) and
`api_admin_ai_review_weekly_trigger` (admin POST) call it — and the
deploy script zips each lambda dir in isolation. The only shared
runtime surface across lambdas is the common layer.

Flow (see `docs/features/ai-review/f3-weekly/PLAN.md`):

1. Resolve the active whitelisted league.
2. Resolve the week to recap (caller override OR `nfl_state.week - 1`,
   clamped to >= 1).
3. Pre-flight: confirm `season_type` is `regular` or `post` (unless
   `use_previous_season=True` for dry-run calibration).
4. Idempotency: bail with `ReportAlreadyExistsError` if a row exists
   for `(league_id, "weekly", "<season>W<NN>")` and `force=False`.
5. Pull data — matchups for this week, rosters + users, last N
   memories. For `use_previous_season=True`, swap matchups + rosters
   + users to the prior league.
6. Build the system + user prompts, call `claude_helper.generate`.
7. Parse Claude's JSON envelope. On parse failure, persist the raw
   response as `body_markdown` and skip memory append (so the recap
   isn't lost).
8. Persist via `ai_reports_store.write_report` with token-usage
   metadata + memory counts.
9. Write each new memory via `ai_memories_store.write_memory`.
10. Deliver via SES + SNS (single admin for dry-run, all 12 for
    broadcast). Stamp `metadata.broadcast_at` on the non-dry-run path.

Returns a dict suitable for JSON-encoding in the API response.
"""
from __future__ import annotations

import json
from typing import Any

from lambdas.common import (
    ai_memories_store,
    ai_reports_store,
    claude_helper,
)
from lambdas.common.constants import (
    ADMIN_DOMINICK_USER_ID,
    AI_REVIEW_WEEKLY_DELIVER,
    AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES,
    AI_REVIEW_WEEKLY_MAX_TOKENS,
    AI_REVIEW_WEEKLY_MEMORY_LOOKBACK,
    AI_REVIEW_WEEKLY_MODEL,
    AI_REVIEW_WEEKLY_OK_SEASON_TYPES,
    AI_REVIEW_WEEKLY_PROMPT_VERSION,
    CLT_LEAGUE_ID,
)
from lambdas.common.email_templates.ai_review import (
    build_email_payload,
    render_preview_for_user,
)
from lambdas.common.errors import (
    DoNotBroadcastError,
    NotFoundError,
    ReportAlreadyExistsError,
    ValidationError,
)
from lambdas.common.logger import get_logger
from lambdas.common.season_resolver import resolve_historical_league
from lambdas.common.ses_helper import send_emails_concurrently
from lambdas.common.nfl_news import fetch_nfl_news, format_news_for_prompt
from lambdas.common.sleeper_helper import (
    get_nfl_state,
    get_previous_league_id,
    get_sleeper_league,
    get_sleeper_league_matchups,
    get_sleeper_league_rosters,
    get_sleeper_league_users,
)
from lambdas.common.sns_helper import send_push_to_users
from lambdas.common.supabase_helper import (
    get_active_whitelisted_league,
    get_active_whitelisted_users,
)
from lambdas.common.weekly_prompts import (
    build_system_blocks,
    build_user_prompt,
)

log = get_logger(__file__)

REPORT_TYPE = "weekly"
PUSH_TITLE_BROADCAST = "This week's AI recap is in"
PUSH_BODY_BROADCAST = "Tap to see who got roasted this week."
PUSH_TITLE_DRY_RUN = "[DRY RUN] Weekly AI recap ready"
PUSH_BODY_DRY_RUN = "Tap to preview before broadcasting."
PUSH_CATEGORY = "AI_REVIEW"

# Sentinel sentiments accepted on incoming memories from Claude. Any
# other value gets dropped with a warning at write-time.
_VALID_SENTIMENTS: tuple[str, ...] = ("roast", "praise", "lore")
_MEMORY_TEXT_MAX = 200


def run_weekly(
    *,
    week: int | None = None,
    dry_run: bool = True,
    force: bool = False,
    seasons_back: int = 0,
    use_previous_season: bool = False,
    created_by_user_id: str | None = None,
) -> dict[str, Any]:
    """Drive the weekly AI Review generation + delivery.

    Args:
        week: NFL week to recap (1-22). When None, resolves from
            `nfl_state.week - 1` (the just-completed week).
        dry_run: When True (default), the report is still written
            to Dynamo but only delivered to `ADMIN_DOMINICK_USER_ID`
            for tone calibration. When False, broadcasts to all 12.
        force: When False (default), an existing report for this
            (league, "weekly", period) raises
            `ReportAlreadyExistsError`. When True, overwrites.
        seasons_back: How many times to walk `previous_league_id`
            from the active league. 0 (default) = current season,
            1 = prior, 2 = two seasons ago, etc. Used for historical
            backfill. The matchups + rosters + users are all fetched
            from the resolved historical league, and the period is
            built from THAT league's `season` field
            (e.g. `2024W05`). Idempotency keys are still
            (active league_id, "weekly", historical period).
        use_previous_season: DEPRECATED alias for `seasons_back=1`.
            When passed (truthy), maps to seasons_back=1 and logs a
            deprecation warning. Kept for back-compat with the F3
            plan + any in-flight clients. New callers should use
            `seasons_back` instead.
        created_by_user_id: Sleeper user_id of the caller (admin
            trigger) or None (cron). Stamped into metadata for
            auditability.

    Returns:
        Dict carrying `status`, `report`, `dry_run`, `delivery_count`,
        `model`, `token_usage`, `week`, `period`, `memory_count_in`,
        `memory_count_out`, `seasons_back`, `use_previous_season`
        (back-compat). Both handlers wrap this for their responses.

    Raises:
        ReportAlreadyExistsError: Existing row + force=False.
        NotFoundError: No active whitelisted league configured, or
            `seasons_back` exceeds the available chain depth.
        ValidationError: Bad week or seasons_back input.
        ClaudeAPIError: Anthropic call failed after retries.
    """
    # Back-compat: use_previous_season=True is equivalent to
    # seasons_back=1. Only honor the alias when the caller didn't
    # also pass a non-default seasons_back.
    if use_previous_season:
        if seasons_back == 0:
            log.warning(
                "weekly_orchestrator: `use_previous_season` is "
                "deprecated; use `seasons_back=1` instead"
            )
            seasons_back = 1
        else:
            log.warning(
                "weekly_orchestrator: both `use_previous_season` and "
                f"`seasons_back={seasons_back}` provided; honoring "
                "`seasons_back`"
            )

    if seasons_back < 0:
        raise ValidationError(
            message=(
                f"seasons_back must be >= 0, got {seasons_back}"
            ),
            handler="notif_ai_review_weekly",
            function="run_weekly",
            field="seasons_back",
        )

    league_row = get_active_whitelisted_league()
    if not league_row:
        raise NotFoundError(
            message="No active whitelisted league configured",
            handler="notif_ai_review_weekly",
            function="run_weekly",
            resource="whitelisted_leagues",
        )
    league_id = (
        league_row.get("sleeper_league_id")
        or league_row.get("league_id")
        or ""
    )
    if not league_id:
        raise NotFoundError(
            message="Active league row is missing sleeper_league_id",
            handler="notif_ai_review_weekly",
            function="run_weekly",
            resource="whitelisted_leagues",
        )

    nfl_state = get_nfl_state() or {}
    season_type = (nfl_state.get("season_type") or "").lower()
    nfl_season = str(nfl_state.get("season") or "")
    nfl_week_raw = nfl_state.get("week")

    # --- resolve which week to recap -----------------------------------------
    resolved_week = _resolve_week(week=week, nfl_week=nfl_week_raw)

    # --- pre-flight ----------------------------------------------------------
    # For backfill against finished seasons we deliberately bypass
    # the season-type gate (calibrating / archiving against
    # completed data).
    if seasons_back == 0:
        if season_type and season_type not in AI_REVIEW_WEEKLY_OK_SEASON_TYPES:
            log.info(
                f"notif_ai_review_weekly: season_type={season_type!r} "
                f"is not in {AI_REVIEW_WEEKLY_OK_SEASON_TYPES} — "
                f"skipping (off-season / preseason no-op)"
            )
            return {
                "status": "skipped_offseason",
                "report": None,
                "dry_run": dry_run,
                "delivery_count": 0,
                "model": AI_REVIEW_WEEKLY_MODEL,
                "token_usage": None,
                "week": resolved_week,
                "period": _period(resolved_week, nfl_season),
                "memory_count_in": 0,
                "memory_count_out": 0,
                "skipped": True,
                "reason": f"season_type={season_type!r}",
                "seasons_back": seasons_back,
                "use_previous_season": seasons_back == 1,
                # Admin Portal F2 — never previews on the no-op path.
                "previews": None,
            }

    # --- data fetch source league --------------------------------------------
    matchup_league_id = league_id
    if seasons_back == 0:
        matchup_league_id = _current_season_league_id(league_id)
    if seasons_back > 0:
        historical_league = resolve_historical_league(
            league_id,
            seasons_back,
            league_fetcher=get_sleeper_league,
        )
        prior_id = (
            historical_league.get("league_id")
            or str(historical_league.get("league_id") or "")
        )
        if not prior_id:
            raise NotFoundError(
                message=(
                    f"Resolved historical league for seasons_back="
                    f"{seasons_back} is missing a league_id"
                ),
                handler="notif_ai_review_weekly",
                function="run_weekly",
                resource="previous_league_id",
            )
        matchup_league_id = prior_id
        log.info(
            f"notif_ai_review_weekly: backfill seasons_back="
            f"{seasons_back} -> league {prior_id} (week "
            f"{resolved_week})"
        )

    league = get_sleeper_league(matchup_league_id)
    league_name = (
        league.get("name")
        or league_row.get("league_name")
        or "League"
    )
    season = str(
        league.get("season") or nfl_season or "2026"
    )
    period = _period(resolved_week, season)

    # --- idempotency ---------------------------------------------------------
    existing = ai_reports_store.get_report(
        league_id=league_id, report_type=REPORT_TYPE, period=period
    )
    if existing and not force:
        raise ReportAlreadyExistsError(
            message=(
                f"A weekly report already exists for period {period}"
            ),
            handler="notif_ai_review_weekly",
            existing={
                "league_id": existing.get("league_id"),
                "report_type": existing.get("report_type"),
                "period": existing.get("period"),
                "created_at": existing.get("created_at"),
            },
        )

    # --- data load -----------------------------------------------------------
    matchups_raw = get_sleeper_league_matchups(
        matchup_league_id, resolved_week
    ) or []
    sleeper_users = get_sleeper_league_users(matchup_league_id) or []
    rosters = get_sleeper_league_rosters(matchup_league_id) or []

    matchups = _build_matchups(
        matchups_raw=matchups_raw,
        rosters=rosters,
        sleeper_users=sleeper_users,
    )

    # iOS surface (`MatchupBlurbCardView` on `MatchupsView`) decodes
    # `metadata.matchups[]` per weekly report and renders the blurb
    # under each matchup card. Built deterministically here so future
    # live-week cron fires emit the same shape as the backfilled
    # historical reports — see PR #91 / iOS `WeeklyMatchupBlurb`.
    matchup_blurbs = _build_matchup_blurbs(
        matchups_raw=matchups_raw,
        rosters=rosters,
        sleeper_users=sleeper_users,
    )

    prior_memories = ai_memories_store.list_recent_memories(
        league_id, season, limit=AI_REVIEW_WEEKLY_MEMORY_LOOKBACK
    )

    # --- fetch live NFL news (best-effort) -----------------------------------
    rostered_player_ids: set[str] = set()
    for roster in rosters:
        for pid in roster.get("players") or []:
            rostered_player_ids.add(str(pid))
    news_data = fetch_nfl_news(rostered_player_ids=rostered_player_ids)
    nfl_news_block = format_news_for_prompt(news_data)

    # --- prompts + generate --------------------------------------------------
    system_blocks = build_system_blocks()
    user_prompt = build_user_prompt(
        league_name=league_name,
        season=season,
        week=resolved_week,
        matchups=matchups,
        prior_memories=prior_memories,
        nfl_news=nfl_news_block,
    )

    raw_text, token_usage = claude_helper.generate(
        prompt=user_prompt,
        system=system_blocks,
        model=AI_REVIEW_WEEKLY_MODEL,
        max_tokens=AI_REVIEW_WEEKLY_MAX_TOKENS,
        return_usage=True,
    )

    # --- parse JSON envelope -------------------------------------------------
    body_markdown, parsed_memories, parse_ok = _parse_envelope(raw_text)

    # --- persist report ------------------------------------------------------
    metadata: dict[str, Any] = {
        "dry_run": dry_run,
        "force": force,
        "seasons_back": seasons_back,
        # Back-compat tag — present for any downstream consumers that
        # read this field. True when seasons_back >= 1.
        "use_previous_season": seasons_back >= 1,
        "model": AI_REVIEW_WEEKLY_MODEL,
        "prompt_version": AI_REVIEW_WEEKLY_PROMPT_VERSION,
        "token_usage": token_usage,
        "nfl_season": nfl_season,
        "nfl_season_type": season_type,
        "matchup_league_id": matchup_league_id,
        "week": resolved_week,
        "memory_count_in": len(prior_memories),
        "memory_count_out": 0,  # patched below post-write
        "envelope_parsed": parse_ok,
        "broadcast_at": None,
        # Per-matchup blurbs for the iOS MatchupBlurbCardView surface.
        # Empty list is safe (off-week / no matchups) — the iOS
        # decoder treats a missing/empty array as "no blurbs to
        # render" and the surface no-ops.
        "matchups": matchup_blurbs,
    }
    if created_by_user_id:
        metadata["created_by_user_id"] = created_by_user_id

    report_row = ai_reports_store.write_report(
        league_id=league_id,
        report_type=REPORT_TYPE,
        period=period,
        body_markdown=body_markdown,
        metadata=metadata,
    )

    # --- write memories ------------------------------------------------------
    written_memories = 0
    if parse_ok and parsed_memories:
        # Capped at AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES at parse time.
        for mem in parsed_memories:
            try:
                ai_memories_store.write_memory(
                    league_id=league_id,
                    season=season,
                    week=resolved_week,
                    text=mem["text"],
                    sentiment=mem["sentiment"],
                    manager_user_id=mem.get("manager_user_id"),
                )
                written_memories += 1
            except Exception as err:  # noqa: BLE001 — memories are best-effort
                log.warning(
                    f"notif_ai_review_weekly: write_memory failed "
                    f"(text={mem.get('text', '')[:60]!r}): {err}"
                )

        if written_memories:
            try:
                ai_reports_store.update_metadata(
                    league_id=league_id,
                    report_type=REPORT_TYPE,
                    period=period,
                    partial={"memory_count_out": written_memories},
                )
                metadata["memory_count_out"] = written_memories
                report_row["metadata"] = metadata
            except Exception as err:  # noqa: BLE001 — non-blocking
                log.warning(
                    f"notif_ai_review_weekly: update_metadata "
                    f"memory_count_out failed: {err}"
                )

    if not AI_REVIEW_WEEKLY_DELIVER:
        log.info(
            f"notif_ai_review_weekly: delivery off — stored {period}, "
            f"no email or push"
        )
        return {
            "status": "stored",
            "report_id": report_row.get("sk"),
            "report": report_row,
            "dry_run": dry_run,
            "delivery_count": 0,
            "model": AI_REVIEW_WEEKLY_MODEL,
            "token_usage": token_usage,
            "week": resolved_week,
            "period": period,
            "memory_count_in": len(prior_memories),
            "memory_count_out": written_memories,
            "envelope_parsed": parse_ok,
            "seasons_back": seasons_back,
            "use_previous_season": seasons_back >= 1,
            "previews": None,
        }

    # --- pre-broadcast DNB check (Admin Portal F3) ---------------------------
    # Re-read the row right before SES fan-out so admins who flipped
    # `do_not_broadcast=true` AFTER generation but BEFORE broadcast
    # still get the abort. Dry-run path skips — locking only matters
    # for real-broadcast attempts.
    if not dry_run:
        _enforce_not_dnb(league_id=league_id, period=period)

    # --- deliver -------------------------------------------------------------
    delivery_count = _deliver(
        dry_run=dry_run,
        league_id=league_id,
        league_name=league_name,
        body_markdown=body_markdown,
        period=period,
        week=resolved_week,
    )

    # --- previews (Admin Portal F2) ------------------------------------------
    # Threads the resolved `week` so the subject line reads
    # "<Name>, your Week N Weekly Review".
    previews = _build_previews(
        dry_run=dry_run,
        body_markdown=body_markdown,
        league_name=league_name,
        week=resolved_week,
    )
    if previews is not None:
        print(
            f"[weekly-trigger] preview generated for {len(previews)} "
            f"users (dry_run=true, week={resolved_week})"
        )

    # --- stamp broadcast_at on broadcast path AFTER SES success --------------
    # Partial-failure safe: only runs if `_deliver` returned without
    # raising. Uses the canonical helper so all three orchestrators
    # write the same key with the same format.
    if not dry_run:
        try:
            updated = ai_reports_store.stamp_broadcast_at(
                league_id=league_id,
                report_type=REPORT_TYPE,
                period=period,
            )
            if isinstance(updated, dict) and updated.get("metadata"):
                metadata.update(updated["metadata"])
                report_row["metadata"] = metadata
        except Exception as err:  # noqa: BLE001 — non-blocking
            log.warning(
                f"notif_ai_review_weekly: stamp_broadcast_at failed: {err}"
            )

    status = "dry_run_sent" if dry_run else "broadcast"
    return {
        "status": status,
        "report_id": report_row.get("sk"),
        "report": report_row,
        "dry_run": dry_run,
        "delivery_count": delivery_count,
        "model": AI_REVIEW_WEEKLY_MODEL,
        "token_usage": token_usage,
        "week": resolved_week,
        "period": period,
        "memory_count_in": len(prior_memories),
        "memory_count_out": written_memories,
        "envelope_parsed": parse_ok,
        "seasons_back": seasons_back,
        # Back-compat surface so the API response shape stays stable
        # for clients still reading `use_previous_season`.
        "use_previous_season": seasons_back >= 1,
        # Admin Portal F2 — populated on dry-run only.
        "previews": previews,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _current_season_league_id(league_id: str) -> str:
    """whitelisted_leagues still holds CLT's 2025 id, and reports + memories
    stay keyed on it so the site's archive is continuous. Sleeper minted a new
    id when the league renewed, so this season's matchups come from
    CLT_LEAGUE_ID when it is the renewal of the whitelisted league."""
    if league_id == CLT_LEAGUE_ID:
        return league_id
    current = get_sleeper_league(CLT_LEAGUE_ID) or {}
    if str(current.get("previous_league_id") or "") == league_id:
        return CLT_LEAGUE_ID
    return league_id


def _enforce_not_dnb(*, league_id: str, period: str) -> None:
    """Re-read the just-written report row and raise
    `DoNotBroadcastError` (HTTP 409) when `metadata.do_not_broadcast`
    is truthy. Called immediately before SES fan-out on the real-
    broadcast path only — dry-run delivery is unaffected.

    Cheap: one Dynamo `get_item`. Catches the race where an admin
    flips DNB on between `write_report` and the broadcast attempt.
    """
    fresh = ai_reports_store.get_report(
        league_id=league_id,
        report_type=REPORT_TYPE,
        period=period,
    )
    if not fresh:
        return
    meta = fresh.get("metadata") or {}
    flag = meta.get("do_not_broadcast")
    if flag is True or (isinstance(flag, str) and flag.lower() == "true"):
        raise DoNotBroadcastError(
            handler="notif_ai_review_weekly",
            report_type=REPORT_TYPE,
            period=period,
        )


def _resolve_week(*, week: int | None, nfl_week: Any) -> int:
    """Return the week to recap. Caller override takes precedence;
    otherwise `nfl_state.week - 1`, clamped to >= 1. Week 22 is the
    upper bound (regular season + playoffs)."""
    if week is not None:
        try:
            value = int(week)
        except (TypeError, ValueError) as err:
            raise ValidationError(
                message=f"Invalid week override: {week!r}",
                handler="notif_ai_review_weekly",
                function="_resolve_week",
                field="week",
            ) from err
        if value < 1 or value > 22:
            raise ValidationError(
                message=(
                    f"week override must be in 1..22, got {value}"
                ),
                handler="notif_ai_review_weekly",
                function="_resolve_week",
                field="week",
            )
        return value

    try:
        nfl_week_value = int(nfl_week or 1)
    except (TypeError, ValueError):
        nfl_week_value = 1
    candidate = nfl_week_value - 1
    return max(1, candidate)


def _period(week: int, season: str | int) -> str:
    """Build the SK-suffix period string. e.g. (4, "2026") ->
    "2026W04". Falls back to "2026" when the season string isn't a
    year-shaped value."""
    season_str = str(season) if season else "2026"
    # Trim a possible PRESEASON-style suffix back to year.
    if "-" in season_str:
        season_str = season_str.split("-")[0]
    if not season_str.isdigit():
        season_str = "2026"
    return f"{season_str}W{int(week):02d}"


def _build_matchups(
    *,
    matchups_raw: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    sleeper_users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair Sleeper matchup entries by `matchup_id` into a list of
    head-to-heads sorted by descending margin. Each entry carries
    `winner`, `loser`, `margin`, `is_tie`, plus bench-point hints
    pulled from `players_points` minus `starters_points`."""
    users_by_id = {u.get("user_id"): u for u in sleeper_users}
    roster_by_id = {r.get("roster_id"): r for r in rosters}

    def _side(entry: dict[str, Any]) -> dict[str, Any]:
        roster_id = entry.get("roster_id")
        roster = roster_by_id.get(roster_id, {}) if roster_id else {}
        owner_id = roster.get("owner_id")
        user = users_by_id.get(owner_id, {}) if owner_id else {}
        team_name = (user.get("metadata") or {}).get("team_name")
        display_name = user.get("display_name") or "Unknown"

        points = float(entry.get("points") or 0.0)
        starters_points = entry.get("starters_points") or []
        players_points = entry.get("players_points") or {}

        # Bench points = sum of all players_points minus the
        # starters_points list. Robust against missing fields.
        try:
            all_pp_total = sum(
                float(v or 0.0) for v in players_points.values()
            )
        except Exception:  # noqa: BLE001 — defensive
            all_pp_total = points
        try:
            starters_total = sum(
                float(v or 0.0) for v in starters_points
            )
        except Exception:  # noqa: BLE001 — defensive
            starters_total = points
        bench_points = max(0.0, all_pp_total - starters_total)

        return {
            "roster_id": roster_id,
            "user_id": owner_id,
            "manager_display_name": display_name,
            "team_name": team_name,
            "points": round(points, 2),
            "bench_points": round(bench_points, 2),
        }

    pairs: dict[int, list[dict[str, Any]]] = {}
    for entry in matchups_raw:
        mid = entry.get("matchup_id")
        if mid is None:
            continue
        pairs.setdefault(mid, []).append(entry)

    matchups: list[dict[str, Any]] = []
    for mid, group in pairs.items():
        if len(group) != 2:
            continue
        a, b = group
        a_side = _side(a)
        b_side = _side(b)
        if a_side["points"] >= b_side["points"]:
            winner, loser = a_side, b_side
        else:
            winner, loser = b_side, a_side
        margin = round(winner["points"] - loser["points"], 2)
        matchups.append(
            {
                "matchup_id": mid,
                "winner": winner,
                "loser": loser,
                "margin": margin,
                "is_tie": abs(margin) < 0.0001,
            }
        )

    matchups.sort(key=lambda m: float(m.get("margin") or 0.0), reverse=True)
    return matchups


# ---------------------------------------------------------------------------
# Per-matchup blurbs (iOS `MatchupBlurbCardView` wire shape)
# ---------------------------------------------------------------------------
#
# The historical 36-report backfill stamped `metadata.matchups[]` on
# each weekly row by hand. The live cron + admin trigger must emit
# the same shape so the iOS surface keeps rendering once the 2026
# season opens. Generated DETERMINISTICALLY in Python (no LLM call)
# from the same Sleeper data fetched for the prompt — see iOS
# `WeeklyMatchupBlurb` decoder for the contract.


# Margin bands -> template format strings. Tuple-ordered: the first
# band whose threshold the margin meets wins. Sentinel `0.0` floor
# catches ties via the explicit `winner == "tie"` short-circuit.
# Keep the templates aligned with the backfilled historical voice.
_BLURB_BANDS: tuple[tuple[float, str], ...] = (
    (
        60.0,
        "**{winner_team} obliterated {loser_team} {wpts}-{lpts}** — "
        "a {margin}-point shellacking that ended the conversation by "
        "halftime.",
    ),
    (
        40.0,
        "**{winner_team} blew out {loser_team} {wpts}-{lpts}** — "
        "{margin}-point margin, no nail-biting required.",
    ),
    (
        20.0,
        "**{winner_team} handled {loser_team} {wpts}-{lpts}** — "
        "comfortable {margin}-point win, never in doubt.",
    ),
    (
        8.0,
        "**{winner_team} edged {loser_team} {wpts}-{lpts}** — "
        "{margin}-point swing, one MNF performance away from "
        "flipping.",
    ),
    (
        3.0,
        "**{winner_team} squeaked past {loser_team} {wpts}-{lpts}** "
        "— a {margin}-point grinder. Both managers ran out the clock "
        "with bench-checks.",
    ),
    (
        0.0,
        "**{winner_team} stole one from {loser_team} {wpts}-{lpts}** "
        "— {margin}-point heartbreaker. {loser_handle} is reading "
        "this with clenched jaw.",
    ),
)

_BLURB_TIE_TEMPLATE = (
    "**{team_a} {apts} TIED {team_b} {bpts}** — both teams stuck "
    "the landing on the same number."
)


def _fmt_score(value: Any) -> str:
    """Render a score as a one-decimal string (matches backfilled
    template style: "204.9", "143.82" rounded to one place for
    readability inside the blurb)."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def render_matchup_blurb(matchup: dict[str, Any]) -> str:
    """Render a deterministic one-line markdown blurb for a single
    matchup. Pure function — no I/O, no LLM call.

    Args:
        matchup: Dict carrying at minimum `team_a`, `team_b`,
            `handle_a`, `handle_b`, `score_a`, `score_b`, `margin`,
            `winner` ("a" | "b" | "tie"). Scores + margin may be
            floats OR strings (boto3-safe wire shape).

    Returns:
        Markdown blurb string. Always non-empty.
    """
    team_a = str(matchup.get("team_a") or "Team A")
    team_b = str(matchup.get("team_b") or "Team B")
    handle_a = str(matchup.get("handle_a") or team_a)
    handle_b = str(matchup.get("handle_b") or team_b)
    winner = str(matchup.get("winner") or "tie").lower()

    try:
        score_a = float(matchup.get("score_a") or 0.0)
    except (TypeError, ValueError):
        score_a = 0.0
    try:
        score_b = float(matchup.get("score_b") or 0.0)
    except (TypeError, ValueError):
        score_b = 0.0
    try:
        margin = float(matchup.get("margin") or 0.0)
    except (TypeError, ValueError):
        margin = 0.0

    if winner == "tie":
        return _BLURB_TIE_TEMPLATE.format(
            team_a=team_a,
            team_b=team_b,
            apts=_fmt_score(score_a),
            bpts=_fmt_score(score_b),
        )

    if winner == "a":
        winner_team, loser_team = team_a, team_b
        winner_handle, loser_handle = handle_a, handle_b
        wpts, lpts = score_a, score_b
    else:
        winner_team, loser_team = team_b, team_a
        winner_handle, loser_handle = handle_b, handle_a
        wpts, lpts = score_b, score_a

    abs_margin = abs(margin)
    template = _BLURB_BANDS[-1][1]
    for threshold, candidate in _BLURB_BANDS:
        if abs_margin >= threshold:
            template = candidate
            break

    return template.format(
        winner_team=winner_team,
        loser_team=loser_team,
        winner_handle=winner_handle,
        loser_handle=loser_handle,
        wpts=_fmt_score(wpts),
        lpts=_fmt_score(lpts),
        margin=_fmt_score(abs_margin),
    )


def _build_matchup_blurbs(
    *,
    matchups_raw: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    sleeper_users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the iOS `metadata.matchups[]` wire shape from the same
    raw Sleeper inputs used by `_build_matchups`. Preserves the
    insertion order of the two sides per `matchup_id` as `team_a`
    (first roster entry seen) and `team_b` (second), which keeps the
    `winner` axis stable across re-runs.

    Returns one dict per fully-paired matchup. Solo / un-paired
    Sleeper entries (e.g., bye-week placeholders) are skipped — same
    rule as `_build_matchups`. Empty list is a valid return.

    Wire shape per entry (boto3-safe — scores + margin as strings):

        {
            "matchup_id": int,
            "team_a": str, "team_b": str,
            "handle_a": str, "handle_b": str,
            "user_id_a": str, "user_id_b": str,
            "score_a": str, "score_b": str,
            "margin": str,
            "winner": "a" | "b" | "tie",
            "blurb": str,
        }
    """
    users_by_id = {u.get("user_id"): u for u in sleeper_users}
    roster_by_id = {r.get("roster_id"): r for r in rosters}

    def _side_view(entry: dict[str, Any]) -> dict[str, Any]:
        roster_id = entry.get("roster_id")
        roster = roster_by_id.get(roster_id, {}) if roster_id else {}
        owner_id = roster.get("owner_id") or ""
        user = users_by_id.get(owner_id, {}) if owner_id else {}
        team_name = (user.get("metadata") or {}).get("team_name") or (
            user.get("display_name") or "Unknown"
        )
        handle = user.get("display_name") or "Unknown"
        try:
            points = float(entry.get("points") or 0.0)
        except (TypeError, ValueError):
            points = 0.0
        return {
            "team": team_name,
            "handle": handle,
            "user_id": str(owner_id) if owner_id else "",
            "points": round(points, 2),
        }

    pairs: dict[int, list[dict[str, Any]]] = {}
    for entry in matchups_raw:
        mid = entry.get("matchup_id")
        if mid is None:
            continue
        pairs.setdefault(mid, []).append(entry)

    blurbs: list[dict[str, Any]] = []
    for mid, group in pairs.items():
        if len(group) != 2:
            continue
        a_raw, b_raw = group
        a = _side_view(a_raw)
        b = _side_view(b_raw)
        margin_signed = round(a["points"] - b["points"], 2)
        is_tie = abs(margin_signed) < 0.0001
        if is_tie:
            winner = "tie"
        elif margin_signed > 0:
            winner = "a"
        else:
            winner = "b"
        margin_abs = round(abs(margin_signed), 2)

        entry_dict: dict[str, Any] = {
            "matchup_id": mid,
            "team_a": a["team"],
            "team_b": b["team"],
            "handle_a": a["handle"],
            "handle_b": b["handle"],
            "user_id_a": a["user_id"],
            "user_id_b": b["user_id"],
            "score_a": str(a["points"]),
            "score_b": str(b["points"]),
            "margin": str(margin_abs),
            "winner": winner,
        }
        entry_dict["blurb"] = render_matchup_blurb(entry_dict)
        blurbs.append(entry_dict)

    return blurbs


def _parse_envelope(
    raw_text: str,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Parse Claude's response as the F3 JSON envelope.

    Returns a tuple of `(body_markdown, memories, parse_ok)`.

    - On clean parse with `body_markdown` + `new_memories`: returns
      the markdown and a sanitized memory list, `parse_ok=True`.
    - On `json.JSONDecodeError` or missing `body_markdown`: returns
      the raw text as the markdown body, an empty memory list, and
      `parse_ok=False`. The recap is preserved even if Claude
      drifted away from the JSON contract.
    """
    text = (raw_text or "").strip()
    if not text:
        return "", [], False

    # Claude occasionally wraps the JSON in ```json fences despite
    # the explicit instruction. Strip them defensively.
    fenced = text
    if fenced.startswith("```"):
        fenced = fenced.split("\n", 1)[1] if "\n" in fenced else fenced[3:]
        if fenced.endswith("```"):
            fenced = fenced[:-3]
        fenced = fenced.strip()
        # Drop leading "json" tag if present.
        if fenced.lower().startswith("json\n"):
            fenced = fenced[5:].strip()

    try:
        envelope = json.loads(fenced)
    except json.JSONDecodeError as err:
        log.warning(
            f"notif_ai_review_weekly: failed to parse JSON envelope "
            f"({err}); persisting raw response as markdown"
        )
        return text, [], False

    if not isinstance(envelope, dict):
        log.warning(
            "notif_ai_review_weekly: JSON envelope is not an object; "
            "persisting raw response as markdown"
        )
        return text, [], False

    body_markdown = envelope.get("body_markdown")
    if not isinstance(body_markdown, str) or not body_markdown.strip():
        log.warning(
            "notif_ai_review_weekly: envelope missing body_markdown; "
            "persisting raw response as markdown"
        )
        return text, [], False

    raw_memories = envelope.get("new_memories") or []
    if not isinstance(raw_memories, list):
        raw_memories = []

    sanitized: list[dict[str, Any]] = []
    for entry in raw_memories[:AI_REVIEW_WEEKLY_MAX_NEW_MEMORIES]:
        if not isinstance(entry, dict):
            continue
        mtext = entry.get("text")
        sentiment = entry.get("sentiment")
        manager_user_id = entry.get("manager_user_id")
        if not isinstance(mtext, str) or not mtext.strip():
            continue
        if sentiment not in _VALID_SENTIMENTS:
            log.warning(
                f"notif_ai_review_weekly: dropping memory with bad "
                f"sentiment={sentiment!r}"
            )
            continue
        trimmed = mtext.strip()[:_MEMORY_TEXT_MAX]
        sanitized.append(
            {
                "text": trimmed,
                "sentiment": sentiment,
                "manager_user_id": (
                    manager_user_id
                    if isinstance(manager_user_id, str)
                    and manager_user_id.strip()
                    else None
                ),
            }
        )

    return body_markdown, sanitized, True


def _deliver(
    *,
    dry_run: bool,
    league_id: str,
    league_name: str,
    body_markdown: str,
    period: str,
    week: int,
) -> int:
    """Build per-user email payloads + push notifications and fan
    them out. Returns the number of recipients attempted (not the
    number of successful sends — per-channel success is logged via
    the existing notification_log path)."""
    recipients = _resolve_recipients(dry_run=dry_run)
    if not recipients:
        log.warning(
            f"notif_ai_review_weekly: no recipients resolved "
            f"(dry_run={dry_run}, league={league_id})"
        )
        return 0

    period_label = f"Week {week}"
    deep_link = f"xomper://ai-review/weekly/{period}"
    email_tasks: list[tuple[str, str, str, str]] = []
    push_user_ids: list[str] = []

    for recipient in recipients:
        sleeper_user_id = recipient.get("sleeper_user_id")
        email = recipient.get("email")
        first_name = (
            recipient.get("display_name")
            or recipient.get("sleeper_username")
            or "there"
        )
        if email:
            payload = build_email_payload(
                user_email=email,
                user_first_name=first_name,
                report_type=REPORT_TYPE,
                body_markdown=body_markdown,
                period_label=period_label,
                league_name=league_name,
            )
            subject = payload["subject"]
            if dry_run:
                subject = f"[DRY RUN] {subject}"
            email_tasks.append(
                (
                    email,
                    subject,
                    payload["html_body"],
                    payload["text_body"],
                )
            )
        if sleeper_user_id:
            push_user_ids.append(sleeper_user_id)

    if email_tasks:
        send_emails_concurrently(email_tasks)

    if push_user_ids:
        title = PUSH_TITLE_DRY_RUN if dry_run else PUSH_TITLE_BROADCAST
        body = PUSH_BODY_DRY_RUN if dry_run else PUSH_BODY_BROADCAST
        send_push_to_users(
            push_user_ids,
            title,
            body,
            PUSH_CATEGORY,
            {"url": deep_link, "period": period, "week": week},
        )

    return len(recipients)


def _resolve_recipients(*, dry_run: bool) -> list[dict[str, Any]]:
    """For dry_run, return the single admin row (resolved from the
    whitelisted_users table). For broadcast, return all active
    users."""
    all_users = get_active_whitelisted_users()
    if dry_run:
        admin = next(
            (
                u
                for u in all_users
                if u.get("sleeper_user_id") == ADMIN_DOMINICK_USER_ID
            ),
            None,
        )
        return [admin] if admin else []
    return all_users


def _build_previews(
    *,
    dry_run: bool,
    body_markdown: str,
    league_name: str,
    week: int,
) -> list[dict[str, Any]] | None:
    """Render an email preview row per active whitelisted user.

    Returns:
        On dry-run: list of preview dicts sorted alphabetically by
        `display_name` (one per active user — typically 12).
        On real broadcast: `None`.
    """
    if not dry_run:
        return None

    users = get_active_whitelisted_users() or []
    sorted_users = sorted(
        users,
        key=lambda u: (u.get("display_name") or "").lower(),
    )

    period_label = f"Week {week}"
    return [
        render_preview_for_user(
            user=user,
            report_type=REPORT_TYPE,
            body_markdown=body_markdown,
            period_label=period_label,
            league_name=league_name,
        )
        for user in sorted_users
    ]
