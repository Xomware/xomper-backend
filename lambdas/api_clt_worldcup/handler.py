"""
GET /clt/world-cup - World Cup qualifying standings across CLT's league chain.

No body. Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_member`, which 403s anyone not on the roster.

Records aggregate every divisional regular-season game back through the
`previous_league_id` chain, keyed by franchise (roster_id, which Sleeper keeps
across renewals) and shown under the roster's current owner and team name, so a
taken-over team keeps its history. `userId` is that current owner. Top two per
division qualify; `worldcup_helper` holds the conservative clinch model.

`gamesRemaining` is per division: the most unplayed divisional games any of
its teams has left this season. A pre-draft season has no schedule yet, so it
falls back to the helper's default, as the movement cron does.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from lambdas.common.clt_gate import require_member
from lambdas.common.constants import CLT_LEAGUE_ID, TOTAL_REGULAR_WEEKS
from lambdas.common.errors import NotFoundError, handle_errors
from lambdas.common.sleeper_helper import (
    get_sleeper_league,
    get_sleeper_league_matchups,
    get_sleeper_league_rosters,
    get_sleeper_league_users,
)
from lambdas.common.utility_helpers import success_response
from lambdas.common.worldcup_helper import (
    DEFAULT_GAMES_REMAINING,
    clinch_for_division,
    compute_division_standings,
    division_name_map_from_league,
    gather_chain_matchups,
    get_league_chain,
    team_name_of,
)

HANDLER = "api_clt_worldcup"


def _fetch_weeks(chain: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    # A season is 17 sequential Sleeper calls; across the chain that runs past
    # API Gateway's 29 s limit, so fetch every week at once.
    keys = [
        (league["league_id"], week)
        for league in chain
        for week in range(1, TOTAL_REGULAR_WEEKS + 1)
    ]
    with ThreadPoolExecutor(max_workers=10) as pool:
        weeks = list(pool.map(lambda key: get_sleeper_league_matchups(*key), keys))
    return dict(zip(keys, weeks))


def _by_franchise(
    matchups: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    users: list[dict[str, Any]],
) -> None:
    # The helper keys by owner, as Xomper's movement pushes need; CLT wants the
    # franchise, so rewrite each side to the roster's current owner. A roster
    # with no current owner keeps the owner of that season.
    user_by_id = {u["user_id"]: u for u in users}
    owner_by_roster = {r["roster_id"]: r.get("owner_id") for r in rosters}
    for m in matchups:
        for side in ("team_a", "team_b"):
            owner = owner_by_roster.get(m[f"{side}_roster_id"])
            if not owner:
                continue
            user = user_by_id.get(owner) or {}
            m[f"{side}_user_id"] = owner
            m[f"{side}_username"] = user.get("username") or ""
            m[f"{side}_team_name"] = team_name_of(user)


def _unplayed_divisional_games(matchups: list[dict[str, Any]], league_id: str) -> Counter:
    left: Counter = Counter()
    for m in matchups:
        division = m["team_a_division"]
        if (
            m["league_id"] == league_id
            and not m["is_playoff"]
            and division
            and division == m["team_b_division"]
            and not m["team_a_points"]
            and not m["team_b_points"]
        ):
            left[m["team_a_user_id"]] += 1
            left[m["team_b_user_id"]] += 1
    return left


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    require_member(event)

    chain = get_league_chain(CLT_LEAGUE_ID, fetch_league_fn=get_sleeper_league)
    if not chain:
        raise NotFoundError(
            "CLT league not found on Sleeper",
            handler=HANDLER,
            function="handler",
            resource=CLT_LEAGUE_ID,
        )
    head = chain[0]
    played = [league for league in chain if league.get("status") != "pre_draft"]

    weeks = _fetch_weeks(played)
    matchups = gather_chain_matchups(
        played,
        TOTAL_REGULAR_WEEKS,
        fetch_rosters_fn=get_sleeper_league_rosters,
        fetch_users_fn=get_sleeper_league_users,
        fetch_matchups_fn=lambda league_id, week: weeks[(league_id, week)],
    )
    _by_franchise(
        matchups,
        get_sleeper_league_rosters(head["league_id"]),
        get_sleeper_league_users(head["league_id"]),
    )
    left = _unplayed_divisional_games(matchups, head["league_id"])

    divisions = []
    for division, name, teams in compute_division_standings(
        matchups, division_name_map_from_league(head)
    ):
        if head.get("status") == "pre_draft":
            games_remaining = DEFAULT_GAMES_REMAINING
        else:
            games_remaining = max(left[t.user_id] for t in teams)
        clinch_for_division(teams, games_remaining=games_remaining)
        divisions.append({
            "division": division,
            "name": name,
            "gamesRemaining": games_remaining,
            "teams": [
                {
                    "userId": t.user_id,
                    "username": t.username,
                    "teamName": t.team_name,
                    "wins": t.wins,
                    "losses": t.losses,
                    "ties": t.ties,
                    "pointsFor": round(t.points_for, 2),
                    "pointsAgainst": round(t.points_against, 2),
                    "status": t.clinch_status,
                }
                for t in teams
            ],
        })

    return success_response({
        "leagueId": head["league_id"],
        "season": head.get("season", ""),
        "divisions": divisions,
    })
