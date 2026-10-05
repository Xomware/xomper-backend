"""
Tests for `lambdas.api_clt_worldcup.handler`.

Sleeper is faked with a two-season chain of six rosters in two divisions.
Every expected record below is worked by hand from SCHEDULE, so a change to
how games are counted fails here. Supabase's world_cup_* tables were empty at
export (plan step 0a), so there is no stored oracle to compare against.
"""
from __future__ import annotations

import importlib
import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import CLT_LEAGUE_ID, CLT_MEMBERS_TABLE
from lambdas.common.errors import SleeperAPIError

PREVIOUS_ID = "prev-league"

# week -> [(roster_a, points_a, roster_b, points_b)]. Rosters 1-3 are division
# 1 and 4-6 division 2; any pairing across them is not a World Cup game.
SCHEDULE = {
    PREVIOUS_ID: {
        1: [(1, 100, 2, 90), (4, 80, 5, 70), (3, 50, 6, 60)],
        2: [(1, 110, 3, 100), (2, 95, 4, 90), (5, 88, 6, 77)],
        15: [(1, 200, 2, 10)],
    },
    CLT_LEAGUE_ID: {
        1: [(2, 120, 3, 100), (4, 100, 6, 100)],
        2: [(1, 0, 2, 0), (5, 0, 6, 0), (3, 0, 4, 0)],
        3: [(1, 0, 3, 0), (4, 0, 5, 0), (2, 0, 6, 0)],
    },
}


def league(league_id, status, previous):
    return {
        "league_id": league_id,
        "season": "2026" if league_id == CLT_LEAGUE_ID else "2025",
        "status": status,
        "previous_league_id": previous,
        "metadata": {"division_1": "East", "division_2": "West"},
    }


def rosters(_league_id):
    return [
        {"roster_id": r, "owner_id": f"u{r}", "settings": {"division": 1 if r <= 3 else 2}}
        for r in range(1, 7)
    ]


def users(_league_id):
    return [
        {"user_id": f"u{r}", "username": f"user{r}", "metadata": {"team_name": f"Team {r}"}}
        for r in range(1, 7)
    ]


def matchups(league_id, week):
    out = []
    for matchup_id, (a, a_pts, b, b_pts) in enumerate(SCHEDULE[league_id].get(week, []), 1):
        out.append({"roster_id": a, "matchup_id": matchup_id, "points": a_pts})
        out.append({"roster_id": b, "matchup_id": matchup_id, "points": b_pts})
    return out


@pytest.fixture
def members():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=CLT_MEMBERS_TABLE,
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"email": "member@example.com", "active": True, "sub": "cog-1"})
        yield table


@pytest.fixture
def mod(members, monkeypatch):
    from lambdas.common import clt_gate

    importlib.reload(clt_gate)
    from lambdas.api_clt_worldcup import handler as handler_mod

    handler_mod = importlib.reload(handler_mod)
    leagues = {
        CLT_LEAGUE_ID: league(CLT_LEAGUE_ID, "in_season", PREVIOUS_ID),
        PREVIOUS_ID: league(PREVIOUS_ID, "complete", None),
    }
    monkeypatch.setattr(handler_mod, "get_sleeper_league", leagues.get)
    monkeypatch.setattr(handler_mod, "get_sleeper_league_rosters", rosters)
    monkeypatch.setattr(handler_mod, "get_sleeper_league_users", users)
    monkeypatch.setattr(handler_mod, "get_sleeper_league_matchups", matchups)
    handler_mod.leagues = leagues
    return handler_mod


def event(email="member@example.com"):
    return {
        "httpMethod": "GET",
        "path": "/clt/world-cup",
        "requestContext": {
            "authorizer": {"sub": "cog-1", "email": email, "provider": "cognito", "groups": ""}
        },
    }


def body_of(response):
    return json.loads(response["body"])


def team(user, wins, losses, ties, pf, pa, status):
    r = user[1:]
    return {
        "userId": user,
        "username": f"user{r}",
        "teamName": f"Team {r}",
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "pointsFor": pf,
        "pointsAgainst": pa,
        "status": status,
    }


def test_mid_season_standings_across_the_chain(mod):
    response = mod.handler(event(), None)

    assert response["statusCode"] == 200
    assert body_of(response) == {
        "leagueId": CLT_LEAGUE_ID,
        "season": "2026",
        "divisions": [
            {
                "division": 1,
                "name": "East",
                # Team 1 still has weeks 2 and 3 against division rivals.
                "gamesRemaining": 2,
                "teams": [
                    team("u1", 2, 0, 0, 210, 190, "alive"),
                    team("u2", 1, 1, 0, 210, 200, "alive"),
                    team("u3", 0, 2, 0, 200, 230, "alive"),
                ],
            },
            {
                "division": 2,
                "name": "West",
                "gamesRemaining": 2,
                "teams": [
                    team("u4", 1, 0, 1, 180, 170, "alive"),
                    team("u5", 1, 1, 0, 158, 157, "alive"),
                    team("u6", 0, 1, 1, 177, 188, "alive"),
                ],
            },
        ],
    }


def test_finished_season_settles_clinch_and_elimination(mod, monkeypatch):
    monkeypatch.setitem(SCHEDULE, CLT_LEAGUE_ID, {1: SCHEDULE[CLT_LEAGUE_ID][1]})
    mod.leagues[CLT_LEAGUE_ID]["status"] = "complete"

    divisions = body_of(mod.handler(event(), None))["divisions"]

    assert [d["gamesRemaining"] for d in divisions] == [0, 0]
    assert [t["status"] for t in divisions[0]["teams"]] == ["clinched", "clinched", "eliminated"]
    assert [t["status"] for t in divisions[1]["teams"]] == ["clinched", "clinched", "eliminated"]


def test_pre_draft_season_counts_past_seasons_with_the_default_remaining(mod):
    mod.leagues[CLT_LEAGUE_ID]["status"] = "pre_draft"

    divisions = body_of(mod.handler(event(), None))["divisions"]

    assert [d["gamesRemaining"] for d in divisions] == [6, 6]
    assert [(t["userId"], t["wins"], t["losses"]) for t in divisions[0]["teams"]] == [
        ("u1", 2, 0), ("u3", 0, 1), ("u2", 0, 1),
    ]


def test_non_member_is_refused_before_any_sleeper_call(mod, monkeypatch):
    def no_sleeper(*_):
        raise AssertionError("Sleeper must not be called for a non-member")

    monkeypatch.setattr(mod, "get_sleeper_league", no_sleeper)

    response = mod.handler(event(email="stranger@example.com"), None)

    assert response["statusCode"] == 403


def test_missing_league_is_404(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_sleeper_league", lambda _id: None)

    assert mod.handler(event(), None)["statusCode"] == 404


def test_a_failed_week_fails_the_request_rather_than_skewing_records(mod, monkeypatch):
    def flaky(league_id, week):
        if week == 2:
            raise SleeperAPIError("Error getting league matchups for week 2: HTTP 500")
        return matchups(league_id, week)

    monkeypatch.setattr(mod, "get_sleeper_league_matchups", flaky)

    assert mod.handler(event(), None)["statusCode"] == 502
