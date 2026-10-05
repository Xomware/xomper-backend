"""
Tests for `lambdas.api_clt_proposals.handler`, against moto tables so the
conditional vote put runs with real DynamoDB semantics.

Member 9 is the admin: the Cognito `admin` group plus an `is_admin`
whitelisted_users row, as Xomper's admin gate requires.
"""
from __future__ import annotations

import pytest
from moto import mock_aws

from lambdas.common import admin_gate
from lambdas.common.constants import CLT_LEAGUE_ID
from tests.clt_support import (
    ADMIN_GROUPS,
    body_of,
    create_members,
    create_proposal_tables,
    event,
    member,
)

ADMIN = 9


@pytest.fixture
def tables(monkeypatch):
    admins = {"m9@example.com": {"email": "m9@example.com", "is_admin": True}}
    monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", admins.get)
    with mock_aws():
        members = create_members(member(1), member(2), member(3), member(ADMIN), member(4, active=False))
        proposals, votes = create_proposal_tables()
        yield members, proposals, votes


@pytest.fixture
def handler(tables):
    from lambdas.api_clt_proposals.handler import handler

    return handler


def call(handler, route, body=None, n=1, admin=False):
    method = "GET" if route == "list" else "POST"
    groups = ADMIN_GROUPS if admin else ""
    return handler(event(f"/clt/proposals-{route}", method, body, n=n, groups=groups), None)


def create(handler, n=1, title="Allow IR stash", description="Two IR slots"):
    response = call(handler, "create", {"title": title, "description": description}, n=n)
    assert response["statusCode"] == 201
    return body_of(response)["proposal"]


def test_create_returns_the_open_proposal(handler, tables):
    proposal = create(handler)

    assert proposal["title"] == "Allow IR stash"
    assert proposal["description"] == "Two IR slots"
    assert proposal["status"] == "open"
    assert proposal["proposedBy"] == "Member 1"
    assert proposal["isMine"] is True
    assert (proposal["yesCount"], proposal["noCount"], proposal["myVote"]) == (0, 0, None)
    stored = tables[1].get_item(Key={"id": proposal["id"]})["Item"]
    assert stored["league_id"] == CLT_LEAGUE_ID
    assert stored["proposed_by"] == "m1@example.com"


@pytest.mark.parametrize("body", [
    {"description": "no title"},
    {"title": "   "},
    {"title": "x" * 121},
    {"title": 5},
    {"title": "ok", "description": "x" * 2001},
])
def test_create_rejects_bad_input(handler, body):
    assert call(handler, "create", body)["statusCode"] == 400


def test_list_tallies_votes_by_display_name_and_puts_open_first(handler):
    live = create(handler, n=2, title="Older but open")
    closed = create(handler, title="Newer but closed")
    call(handler, "status", {"proposalId": closed["id"], "status": "closed"}, n=ADMIN, admin=True)
    call(handler, "vote", {"proposalId": live["id"], "vote": "yes"}, n=1)
    call(handler, "vote", {"proposalId": live["id"], "vote": "yes"}, n=3)
    call(handler, "vote", {"proposalId": live["id"], "vote": "no"}, n=2)

    response = call(handler, "list", n=1)

    assert response["statusCode"] == 200
    first, second = body_of(response)["proposals"]
    assert (first["id"], second["id"]) == (live["id"], closed["id"])
    assert first["proposedBy"] == "Member 2"
    assert first["isMine"] is False
    assert (first["yesCount"], first["noCount"], first["myVote"]) == (2, 1, "yes")
    assert sorted(first["voters"]["yes"]) == ["Member 1", "Member 3"]
    assert first["voters"]["no"] == ["Member 2"]
    assert "@" not in response["body"]


def test_a_second_vote_is_refused_and_the_first_stands(handler, tables):
    proposal = create(handler)
    assert call(handler, "vote", {"proposalId": proposal["id"], "vote": "yes"})["statusCode"] == 200

    response = call(handler, "vote", {"proposalId": proposal["id"], "vote": "no"})

    assert response["statusCode"] == 409
    stored = tables[2].get_item(Key={"proposal_id": proposal["id"], "user_id": "m1@example.com"})
    assert stored["Item"]["vote"] == "yes"


def test_voting_on_a_closed_proposal_is_refused(handler):
    proposal = create(handler)
    call(handler, "status", {"proposalId": proposal["id"], "status": "approved"}, n=ADMIN, admin=True)

    response = call(handler, "vote", {"proposalId": proposal["id"], "vote": "yes"}, n=2)

    assert response["statusCode"] == 409


def test_vote_validation_and_missing_proposal(handler):
    proposal = create(handler)

    assert call(handler, "vote", {"proposalId": proposal["id"], "vote": "maybe"})["statusCode"] == 400
    assert call(handler, "vote", {"proposalId": "nope", "vote": "yes"})["statusCode"] == 404


def test_proposer_deletes_their_proposal_and_its_votes(handler, tables):
    proposal = create(handler)
    call(handler, "vote", {"proposalId": proposal["id"], "vote": "yes"}, n=2)

    response = call(handler, "delete", {"proposalId": proposal["id"]}, n=1)

    assert response["statusCode"] == 200
    assert body_of(response) == {"deleted": proposal["id"]}
    assert "Item" not in tables[1].get_item(Key={"id": proposal["id"]})
    assert tables[2].scan()["Count"] == 0


def test_another_member_cannot_delete(handler, tables):
    proposal = create(handler)

    response = call(handler, "delete", {"proposalId": proposal["id"]}, n=2)

    assert response["statusCode"] == 403
    assert "Item" in tables[1].get_item(Key={"id": proposal["id"]})


def test_admin_can_delete_anyones_proposal(handler):
    proposal = create(handler)

    response = call(handler, "delete", {"proposalId": proposal["id"]}, n=ADMIN, admin=True)

    assert response["statusCode"] == 200


def test_admin_sets_status(handler):
    proposal = create(handler)
    call(handler, "vote", {"proposalId": proposal["id"], "vote": "no"}, n=ADMIN)

    response = call(handler, "status", {"proposalId": proposal["id"], "status": "rejected"}, n=ADMIN, admin=True)

    assert response["statusCode"] == 200
    shaped = body_of(response)["proposal"]
    assert shaped["status"] == "rejected"
    assert shaped["voters"]["no"] == ["Member 9"]


def test_status_needs_the_admin_group_and_the_admin_row(handler):
    proposal = create(handler)
    body = {"proposalId": proposal["id"], "status": "approved"}

    # Group without the row, then row without the group.
    assert call(handler, "status", body, n=1, admin=True)["statusCode"] == 403
    assert call(handler, "status", body, n=ADMIN, admin=False)["statusCode"] == 403


def test_status_validation_and_missing_proposal(handler):
    proposal = create(handler)

    bad = call(handler, "status", {"proposalId": proposal["id"], "status": "done"}, n=ADMIN, admin=True)
    missing = call(handler, "status", {"proposalId": "nope", "status": "closed"}, n=ADMIN, admin=True)

    assert bad["statusCode"] == 400
    assert missing["statusCode"] == 404


def test_non_members_and_inactive_members_are_refused(handler):
    assert call(handler, "list", n=4)["statusCode"] == 403
    assert call(handler, "create", {"title": "x"}, n=5)["statusCode"] == 403


def test_wrong_method_for_a_route_is_400(handler):
    response = handler(event("/clt/proposals-create", "GET"), None)

    assert response["statusCode"] == 400
