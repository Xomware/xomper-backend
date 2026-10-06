"""
World Cup standings + clinch helper
====================================
Port of iOS `Xomper/Core/Stores/WorldCupStore.swift` +
`ClinchCalculator.swift`. Pure-logic — accepts pre-fetched matchup
records, returns per-team clinch status keyed by user_id.

Used by `notif_worldcup_movement` to diff week-over-week status
transitions and queue pushes for managers whose clinch state
changed.

The "World Cup" is a cross-season competition aggregating divisional
W/L/PF across the dynasty league chain. Top 2 per division qualify;
points-for is the tiebreaker.

Conservative clinch model — uses wins + games_remaining without
simulating the points-for tiebreaker. Teams tied in wins at the
qualification cutoff stay `alive` until the final week resolves the
tiebreaker. This produces zero false positives — never marks a team
clinched/eliminated when the math hasn't actually decided.
"""
from dataclasses import dataclass, field
from typing import Any

ALIVE = "alive"
CLINCHED = "clinched"
ELIMINATED = "eliminated"
DEFAULT_GAMES_REMAINING = 6
QUALIFY_TOP_N = 2


@dataclass
class TeamRecord:
    user_id: str
    username: str
    team_name: str
    division: int
    division_name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    clinch_status: str = ALIVE


def is_divisional_regular_season_matchup(record: dict[str, Any]) -> bool:
    """Filter for matchup history records that count toward the World
    Cup. Mirrors the iOS `divisionalMatchups` predicate exactly."""
    return (
        not record.get("is_playoff", False)
        and (record.get("team_a_division") or 0) > 0
        and (record.get("team_b_division") or 0) > 0
        and record.get("team_a_division") == record.get("team_b_division")
        and (
            float(record.get("team_a_points") or 0) > 0
            or float(record.get("team_b_points") or 0) > 0
        )
    )


def compute_division_standings(
    matchups: list[dict[str, Any]],
    division_name_map: dict[int, str],
) -> list[tuple[int, str, list[TeamRecord]]]:
    """Aggregate all `matchups` into per-user records, group by
    division, sort each division wins-DESC then PF-DESC, and assign
    clinch status using `clinch_for_division`.

    Returns: list of (division_id, division_name, sorted_teams).
    """
    user_records: dict[str, TeamRecord] = {}

    divisional = [m for m in matchups if is_divisional_regular_season_matchup(m)]

    for m in divisional:
        a_id = m.get("team_a_user_id") or ""
        b_id = m.get("team_b_user_id") or ""
        a_pts = float(m.get("team_a_points") or 0)
        b_pts = float(m.get("team_b_points") or 0)

        rec_a = user_records.setdefault(
            a_id,
            TeamRecord(
                user_id=a_id,
                username=m.get("team_a_username") or "",
                team_name=m.get("team_a_team_name") or "",
                division=int(m.get("team_a_division") or 0),
                division_name="",
            ),
        )
        rec_b = user_records.setdefault(
            b_id,
            TeamRecord(
                user_id=b_id,
                username=m.get("team_b_username") or "",
                team_name=m.get("team_b_team_name") or "",
                division=int(m.get("team_b_division") or 0),
                division_name="",
            ),
        )
        # Refresh display fields from latest record we see.
        if m.get("team_a_team_name"):
            rec_a.team_name = m["team_a_team_name"]
        if m.get("team_b_team_name"):
            rec_b.team_name = m["team_b_team_name"]

        rec_a.points_for += a_pts
        rec_a.points_against += b_pts
        rec_b.points_for += b_pts
        rec_b.points_against += a_pts

        if m.get("winner_roster_id") is None:
            rec_a.ties += 1
            rec_b.ties += 1
        elif a_pts > b_pts:
            rec_a.wins += 1
            rec_b.losses += 1
        else:
            rec_b.wins += 1
            rec_a.losses += 1

    # Resolve display names + group by division.
    divisions: dict[int, list[TeamRecord]] = {}
    for rec in user_records.values():
        rec.division_name = division_name_map.get(rec.division, f"Division {rec.division}")
        divisions.setdefault(rec.division, []).append(rec)

    result: list[tuple[int, str, list[TeamRecord]]] = []
    for division_id in sorted(divisions.keys()):
        teams = sorted(
            divisions[division_id],
            key=lambda t: (-t.wins, -t.points_for),
        )
        result.append((division_id, teams[0].division_name if teams else "", teams))
    return result


def clinch_for_division(
    teams: list[TeamRecord],
    games_remaining: int = DEFAULT_GAMES_REMAINING,
) -> None:
    """Mutates `teams` in place, setting `clinch_status` per team.

    Algorithm (mirrors iOS `ClinchCalculator.calculate`):
    - Top `QUALIFY_TOP_N` (default 2) hold qualifying seats.
    - A qualifying-seat team is `clinched` iff no chaser below the
      cutoff can match its wins even winning every remaining game.
      Otherwise `alive`.
    - A team outside the seats is `eliminated` iff
      `wins + games_remaining < cutoff_wins`. Otherwise `alive`.
    """
    if not teams:
        return

    cutoff_index = QUALIFY_TOP_N - 1
    if len(teams) <= cutoff_index:
        cutoff_wins = teams[0].wins
    else:
        cutoff_wins = teams[cutoff_index].wins

    chasers = teams[cutoff_index + 1:]

    for index, team in enumerate(teams):
        if index <= cutoff_index:
            can_be_caught = any(c.wins + games_remaining >= team.wins for c in chasers)
            team.clinch_status = ALIVE if can_be_caught else CLINCHED
        else:
            max_possible = team.wins + games_remaining
            team.clinch_status = ELIMINATED if max_possible < cutoff_wins else ALIVE


def status_map_from_standings(
    standings: list[tuple[int, str, list[TeamRecord]]],
) -> dict[str, dict[str, Any]]:
    """Flatten the per-division team list into a `user_id → status`
    dict that's cheap to diff against the previous DynamoDB snapshot.
    Status entry includes the rank within division so we can detect
    qualifying-line crossings (rank 2 ↔ rank 3) without re-running
    the standings computation on the diff side.
    """
    out: dict[str, dict[str, Any]] = {}
    for _div_id, _div_name, teams in standings:
        for rank, team in enumerate(teams):
            out[team.user_id] = {
                "status": team.clinch_status,
                "rank": rank,
                "wins": team.wins,
                "division": team.division,
                "team_name": team.team_name,
            }
    return out


def diff_snapshots(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Returns a list of transition events — one per user whose
    status changed since the last snapshot. Includes both clinch
    transitions (alive → clinched / eliminated) and qualifying-line
    crossings (rank 2 ↔ rank 3 across the cutoff).
    """
    events: list[dict[str, Any]] = []
    cutoff_index = QUALIFY_TOP_N - 1
    for user_id, cur in current.items():
        prev = previous.get(user_id)
        if prev is None:
            continue  # first snapshot — no transition

        cur_status = cur["status"]
        prev_status = prev.get("status", ALIVE)
        cur_rank = cur["rank"]
        prev_rank = prev.get("rank", cur_rank)

        if cur_status != prev_status:
            events.append({
                "user_id": user_id,
                "kind": "status",
                "from": prev_status,
                "to": cur_status,
                "team_name": cur.get("team_name", ""),
                "division": cur.get("division", 0),
            })
            continue

        # Cutoff-line flip: rank crossed the qualifying threshold
        # without the clinch status changing (still alive). E.g. you
        # were 2nd → now 3rd, or 3rd → now 2nd.
        crossed_in = prev_rank > cutoff_index and cur_rank <= cutoff_index
        crossed_out = prev_rank <= cutoff_index and cur_rank > cutoff_index
        if crossed_in or crossed_out:
            events.append({
                "user_id": user_id,
                "kind": "line",
                "direction": "in" if crossed_in else "out",
                "team_name": cur.get("team_name", ""),
                "division": cur.get("division", 0),
            })

    return events


def get_league_chain(
    head_league_id: str,
    fetch_league_fn,
) -> list[dict[str, Any]]:
    """Walk `previous_league_id` backward from the head league.
    `fetch_league_fn` is injected so callers can swap in a cached
    Sleeper client for tests; defaults to `get_sleeper_league` in
    production callers.
    """
    chain: list[dict[str, Any]] = []
    current_id: str | None = head_league_id
    while current_id:
        league = fetch_league_fn(current_id)
        if not league:
            break
        chain.append(league)
        current_id = league.get("previous_league_id")
    return chain


def team_name_of(user: dict[str, Any]) -> str:
    return (user.get("metadata") or {}).get("team_name") or user.get("display_name") or ""


def gather_chain_matchups(
    chain: list[dict[str, Any]],
    total_regular_weeks: int,
    fetch_rosters_fn,
    fetch_users_fn,
    fetch_matchups_fn,
    log_fn=None,
) -> list[dict[str, Any]]:
    """Walk every league in `chain`, pull users + rosters + every
    regular-season week, pair matchups by `matchup_id`, and emit
    MatchupHistoryRecord-shaped dicts. Used as input to
    `compute_division_standings`.

    Dependencies are passed in (rather than imported here) so this
    stays free of Sleeper / boto3 coupling — the live callers wire
    in `get_sleeper_league_rosters` / `_users` / `_matchups` from
    `sleeper_helper`. Tests can pass fakes.

    Skips leagues with `status == "pre_draft"` (no games yet).
    """
    records: list[dict[str, Any]] = []
    for league in chain:
        if league.get("status") == "pre_draft":
            continue
        league_id = league["league_id"]
        season = league.get("season", "")
        rosters = fetch_rosters_fn(league_id) or []
        users = fetch_users_fn(league_id) or []
        roster_by_id = {r["roster_id"]: r for r in rosters}
        user_by_id = {u["user_id"]: u for u in users}

        for week in range(1, total_regular_weeks + 1):
            try:
                week_matchups = fetch_matchups_fn(league_id, week)
            except Exception as e:
                if log_fn:
                    log_fn(f"Week {week} matchup fetch failed for {league_id}: {e}")
                continue

            grouped: dict[int, list[dict[str, Any]]] = {}
            for m in week_matchups or []:
                mid = m.get("matchup_id")
                if mid is None:
                    continue
                grouped.setdefault(mid, []).append(m)

            for pair in grouped.values():
                if len(pair) != 2:
                    continue
                a, b = pair
                roster_a = roster_by_id.get(a["roster_id"]) or {}
                roster_b = roster_by_id.get(b["roster_id"]) or {}
                user_a = user_by_id.get(roster_a.get("owner_id") or "") or {}
                user_b = user_by_id.get(roster_b.get("owner_id") or "") or {}
                a_pts = float(a.get("points") or 0)
                b_pts = float(b.get("points") or 0)
                if a_pts > b_pts:
                    winner = a["roster_id"]
                elif b_pts > a_pts:
                    winner = b["roster_id"]
                else:
                    winner = None

                records.append({
                    "league_id": league_id,
                    "season": season,
                    "week": week,
                    "team_a_roster_id": a["roster_id"],
                    "team_a_user_id": roster_a.get("owner_id") or "",
                    "team_a_username": user_a.get("username") or "",
                    "team_a_team_name": team_name_of(user_a),
                    "team_a_division": roster_a.get("settings", {}).get("division") or 0,
                    "team_a_points": a_pts,
                    "team_b_roster_id": b["roster_id"],
                    "team_b_user_id": roster_b.get("owner_id") or "",
                    "team_b_username": user_b.get("username") or "",
                    "team_b_team_name": team_name_of(user_b),
                    "team_b_division": roster_b.get("settings", {}).get("division") or 0,
                    "team_b_points": b_pts,
                    "winner_roster_id": winner,
                    "is_playoff": week > 14,
                })
    return records


def division_name_map_from_league(league: dict[str, Any]) -> dict[int, str]:
    """Sleeper stores division names in `league.metadata.division_1`,
    `division_2`, ... Build a {1: "AFC East", 2: "NFC West", ...} dict.
    """
    metadata = league.get("metadata") or {}
    out: dict[int, str] = {}
    for key, value in metadata.items():
        if not key.startswith("division_"):
            continue
        if not isinstance(value, str) or not value:
            continue
        suffix = key[len("division_"):]
        try:
            num = int(suffix)
        except ValueError:
            continue
        out[num] = value
    return out
