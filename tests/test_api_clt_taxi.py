"""
Tests for `lambdas.api_clt_taxi.handler`. Sleeper rosters are faked; the
steal-request table is moto-backed so the one-request-per-player condition
runs for real.
"""
from __future__ import annotations

import pytest
from moto import mock_aws

from lambdas.common.constants import CLT_LEAGUE_ID
from lambdas.common.errors import SleeperAPIError
from tests.clt_support import body_of, create_members, create_taxi_table, event, member

# Member n owns roster n through Sleeper user u{n}; member 3 co-owns roster 2.
ROSTERS = [
    {"roster_id": 1, "owner_id": "u1", "taxi": ["p-own"]},
    {"roster_id": 2, "owner_id": "u2", "co_owners": ["u3"], "taxi": ["p-taxi", "p-taxi-2"], "players": ["p-active"]},
    {"roster_id": 4, "owner_id": "u4", "taxi": None},
]


@pytest.fixture
def taxi(monkeypatch):
    with mock_aws():
        create_members(member(1), member(2), member(3), member(5, active=False), member(6))
        table = create_taxi_table()
        from lambdas.api_clt_taxi import handler as mod

        monkeypatch.setattr(mod, "get_sleeper_league_rosters", lambda _id: ROSTERS)
        yield mod, table


def request(mod, player_id, n=1):
    return mod.handler(event("/clt/taxi-request", "POST", {"playerId": player_id}, n=n), None)


def test_request_on_another_rosters_taxi_player(taxi):
    mod, table = taxi

    response = request(mod, "p-taxi")

    assert response["statusCode"] == 201
    shaped = body_of(response)["request"]
    assert shaped["playerId"] == "p-taxi"
    assert shaped["rosterId"] == 2
    assert shaped["requestedBy"] == "Member 1"
    assert shaped["isMine"] is True
    stored = table.get_item(Key={"league_id": CLT_LEAGUE_ID, "player_id": "p-taxi"})["Item"]
    assert stored["owner_id"] == "u2"
    assert stored["requested_by"] == "m1@example.com"


def test_second_request_for_the_same_player_is_409(taxi):
    mod, table = taxi
    request(mod, "p-taxi", n=1)

    response = request(mod, "p-taxi", n=6)

    assert response["statusCode"] == 409
    assert table.scan()["Count"] == 1


@pytest.mark.parametrize("player_id, n", [
    ("p-active", 1),   # on a roster, but not its taxi squad
    ("p-nowhere", 1),
    ("p-own", 1),      # caller's own taxi squad
    ("p-taxi", 3),     # co-owner of the roster
    ("", 1),
])
def test_request_is_refused_unless_the_player_is_on_someone_elses_taxi(taxi, player_id, n):
    mod, table = taxi

    assert request(mod, player_id, n=n)["statusCode"] == 400
    assert table.scan()["Count"] == 0


def test_list_shows_requests_newest_first_with_names(taxi):
    mod, _ = taxi
    request(mod, "p-taxi", n=1)
    request(mod, "p-own", n=2)

    response = mod.handler(event("/clt/taxi-list", n=1), None)

    assert response["statusCode"] == 200
    body = body_of(response)
    assert body["leagueId"] == CLT_LEAGUE_ID
    assert [(r["playerId"], r["requestedBy"], r["isMine"]) for r in body["requests"]] == [
        ("p-own", "Member 2", False),
        ("p-taxi", "Member 1", True),
    ]
    assert "@" not in response["body"]


def test_non_member_and_inactive_member_are_refused(taxi):
    mod, _ = taxi

    assert mod.handler(event("/clt/taxi-list", n=4), None)["statusCode"] == 403
    assert request(mod, "p-taxi", n=5)["statusCode"] == 403


def test_sleeper_failure_is_502_and_writes_nothing(taxi, monkeypatch):
    mod, table = taxi

    def down(_id):
        raise SleeperAPIError("Error getting league rosters: HTTP 503")

    monkeypatch.setattr(mod, "get_sleeper_league_rosters", down)

    assert request(mod, "p-taxi")["statusCode"] == 502
    assert table.scan()["Count"] == 0
