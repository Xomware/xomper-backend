"""Tests for `lambdas.api_clt_settings.handler` against a moto settings table."""
from __future__ import annotations

import pytest
from moto import mock_aws

from lambdas.common import admin_gate
from tests.clt_support import (
    ADMIN_GROUPS,
    body_of,
    create_members,
    create_settings_table,
    event,
    member,
)

ADMIN = 9


@pytest.fixture
def handler(monkeypatch):
    admins = {"m9@example.com": {"email": "m9@example.com", "is_admin": True}}
    monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", admins.get)
    with mock_aws():
        create_members(member(1), member(ADMIN))
        create_settings_table()
        from lambdas.api_clt_settings.handler import handler

        yield handler


def get(handler, n=1):
    return handler(event("/clt/settings", n=n), None)


def update(handler, body, n=ADMIN, groups=ADMIN_GROUPS):
    return handler(event("/clt/settings-update", "POST", body, n=n, groups=groups), None)


def test_missing_item_reads_as_off(handler):
    response = get(handler)

    assert response["statusCode"] == 200
    assert body_of(response) == {"emailNotifications": False}


def test_admin_turns_notifications_on_and_off(handler):
    assert body_of(update(handler, {"emailNotifications": True})) == {"emailNotifications": True}
    assert body_of(get(handler)) == {"emailNotifications": True}

    update(handler, {"emailNotifications": False})

    assert body_of(get(handler)) == {"emailNotifications": False}


@pytest.mark.parametrize("body", [{}, {"emailNotifications": "true"}, {"emailNotifications": 1}])
def test_update_needs_a_boolean(handler, body):
    assert update(handler, body)["statusCode"] == 400


def test_non_admin_cannot_update(handler):
    assert update(handler, {"emailNotifications": True}, n=1)["statusCode"] == 403
    assert update(handler, {"emailNotifications": True}, groups="")["statusCode"] == 403
    assert body_of(get(handler)) == {"emailNotifications": False}


def test_non_member_cannot_read(handler):
    assert get(handler, n=2)["statusCode"] == 403
