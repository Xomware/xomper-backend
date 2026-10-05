"""
Tests for `lambdas.api_clt_members.handler` against a moto roster table.
Member 9 is the admin (Cognito `admin` group plus an admin whitelisted row).
"""
from __future__ import annotations

import pytest
from moto import mock_aws

from lambdas.common import admin_gate
from tests.clt_support import ADMIN_GROUPS, body_of, create_members, event, member

ADMIN = 9


@pytest.fixture
def roster(monkeypatch):
    admins = {"m9@example.com": {"email": "m9@example.com", "is_admin": True}}
    monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", admins.get)
    with mock_aws():
        table = create_members(
            member(1),
            member(2, displayName="Alpha", active=False),
            member(ADMIN, displayName="Zed"),
        )
        from lambdas.api_clt_members.handler import handler

        yield handler, table


def update(handler, body, n=ADMIN, groups=ADMIN_GROUPS):
    return handler(event("/clt/members-update", "POST", body, n=n, groups=groups), None)


def row(table, email):
    return table.get_item(Key={"email": email}).get("Item")


def test_admin_lists_every_member_by_name(roster):
    handler, _ = roster

    response = handler(event("/clt/members-list", n=ADMIN, groups=ADMIN_GROUPS), None)

    assert response["statusCode"] == 200
    assert body_of(response)["members"] == [
        {"email": "m2@example.com", "displayName": "Alpha", "role": "member",
         "sleeperUserId": "u2", "active": False, "boundToAccount": True},
        {"email": "m1@example.com", "displayName": "Member 1", "role": "member",
         "sleeperUserId": "u1", "active": True, "boundToAccount": True},
        {"email": "m9@example.com", "displayName": "Zed", "role": "member",
         "sleeperUserId": "u9", "active": True, "boundToAccount": True},
    ]


def test_update_name_and_active(roster):
    handler, table = roster

    response = update(handler, {"email": "M2@example.com ", "displayName": " Beta ", "active": True})

    assert response["statusCode"] == 200
    assert body_of(response)["member"]["displayName"] == "Beta"
    stored = row(table, "m2@example.com")
    assert (stored["displayName"], stored["active"], stored["sub"]) == ("Beta", True, "cog-2")


def test_clear_sub_lets_the_next_sign_in_rebind(roster):
    handler, table = roster

    assert update(handler, {"email": "m1@example.com", "clearSub": True})["statusCode"] == 200
    assert "sub" not in row(table, "m1@example.com")

    # Member 1 signs in from a new Google account with the same email.
    new_account = event("/clt/members-list", n=1)
    new_account["requestContext"]["authorizer"]["sub"] = "cog-new"
    from lambdas.common.clt_gate import require_member

    assert require_member(new_account)["sub"] == "cog-new"


def test_email_change_moves_the_row_and_drops_the_binding(roster):
    handler, table = roster

    response = update(handler, {"email": "m1@example.com", "newEmail": "New1@Example.com"})

    assert response["statusCode"] == 200
    assert row(table, "m1@example.com") is None
    moved = row(table, "new1@example.com")
    assert moved["displayName"] == "Member 1"
    assert "sub" not in moved


def test_email_change_onto_another_member_is_409_and_changes_nothing(roster):
    handler, table = roster

    response = update(handler, {"email": "m1@example.com", "newEmail": "m2@example.com"})

    assert response["statusCode"] == 409
    assert row(table, "m1@example.com")["displayName"] == "Member 1"
    assert row(table, "m2@example.com")["displayName"] == "Alpha"


@pytest.mark.parametrize("body, status", [
    ({"displayName": "x"}, 400),
    ({"email": "m1@example.com"}, 400),
    ({"email": "m1@example.com", "displayName": ""}, 400),
    ({"email": "m1@example.com", "displayName": "x" * 51}, 400),
    ({"email": "m1@example.com", "active": "yes"}, 400),
    ({"email": "m1@example.com", "clearSub": False}, 400),
    ({"email": "m1@example.com", "newEmail": "not-an-email"}, 400),
    ({"email": "ghost@example.com", "active": False}, 404),
])
def test_update_validation(roster, body, status):
    handler, _ = roster

    assert update(handler, body)["statusCode"] == status


def test_non_admin_members_are_refused(roster):
    handler, table = roster

    listing = handler(event("/clt/members-list", n=1), None)
    with_group_only = update(handler, {"email": "m1@example.com", "active": False}, n=1)

    assert listing["statusCode"] == 403
    assert with_group_only["statusCode"] == 403
    assert row(table, "m1@example.com")["active"] is True


def test_admin_without_the_cognito_group_is_refused(roster):
    handler, _ = roster

    response = handler(event("/clt/members-list", n=ADMIN, groups=""), None)

    assert response["statusCode"] == 403
