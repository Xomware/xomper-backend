"""
Admin gate: identity must come from the authorizer context, never the request.

Sleeper ids are public, so a caller-supplied `sleeper_user_id` naming an admin
must not grant anything.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from lambdas.common import admin_gate
from lambdas.common.errors import AuthorizationError

ADMIN_EMAIL = "admin@example.com"
ADMIN_SLEEPER_ID = "594625531702460416"
ADMIN_ROW = {"id": "row-admin", "email": ADMIN_EMAIL, "sleeper_user_id": ADMIN_SLEEPER_ID, "is_admin": True}


def _event(
    *,
    email: str = "",
    groups: str = "",
    sub: str = "sub-1",
    body: dict[str, Any] | None = None,
    authorizer: bool = True,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "httpMethod": "POST",
        "headers": {"X-Sleeper-User-Id": ADMIN_SLEEPER_ID, "X-User-Email": ADMIN_EMAIL},
        "queryStringParameters": {"sleeper_user_id": ADMIN_SLEEPER_ID, "email": ADMIN_EMAIL},
        "body": json.dumps(body or {}),
        "requestContext": {},
    }
    if authorizer:
        event["requestContext"]["authorizer"] = {"sub": sub, "email": email, "provider": "cognito", "groups": groups}
    return event


@pytest.fixture
def whitelist(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    rows = {ADMIN_EMAIL: ADMIN_ROW, "user@example.com": {"id": "row-u", "email": "user@example.com", "is_admin": False}}
    monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", rows.get)

    def _no_sleeper_lookup(*_: Any) -> None:
        raise AssertionError("admin gate must not look up caller-supplied sleeper ids")

    monkeypatch.setattr(
        "lambdas.common.supabase_helper.get_whitelisted_user_by_sleeper_id", _no_sleeper_lookup
    )
    return rows


class TestRequireAdmin:
    def test_admin_token_gets_row(self, whitelist: dict) -> None:
        event = _event(email=ADMIN_EMAIL, groups="us-east-1_x_Google,admin")
        assert admin_gate.require_admin(event) == ADMIN_ROW

    def test_non_admin_token_with_admin_identity_in_request_is_rejected(self, whitelist: dict) -> None:
        body = {"sleeper_user_id": ADMIN_SLEEPER_ID, "email": ADMIN_EMAIL}
        event = _event(email="user@example.com", groups="us-east-1_x_Google", body=body)
        with pytest.raises(admin_gate.NotAdmin):
            admin_gate.require_admin(event, body)

    def test_admin_email_without_admin_group_is_rejected(self, whitelist: dict) -> None:
        # The pool lets a user set their own email unverified, so the email
        # claim matching an admin row is not enough on its own.
        with pytest.raises(admin_gate.NotAdmin):
            admin_gate.require_admin(_event(email=ADMIN_EMAIL, groups=""))

    def test_admin_group_without_admin_row_is_rejected(self, whitelist: dict) -> None:
        with pytest.raises(admin_gate.NotAdmin):
            admin_gate.require_admin(_event(email="user@example.com", groups="admin"))

    def test_admin_group_without_email_is_rejected(self, whitelist: dict) -> None:
        with pytest.raises(admin_gate.NotAdmin):
            admin_gate.require_admin(_event(email="", groups="admin"))

    def test_missing_authorizer_context_is_401(self, whitelist: dict) -> None:
        with pytest.raises(AuthorizationError):
            admin_gate.require_admin(_event(authorizer=False))

    def test_missing_sub_is_401(self, whitelist: dict) -> None:
        with pytest.raises(AuthorizationError):
            admin_gate.require_admin(_event(sub="", email=ADMIN_EMAIL, groups="admin"))


class TestIsAdmin:
    def test_true_for_admin_token(self, whitelist: dict) -> None:
        assert admin_gate.is_admin(_event(email=ADMIN_EMAIL, groups="admin")) is True

    def test_false_for_non_admin_token_claiming_admin(self, whitelist: dict) -> None:
        assert admin_gate.is_admin(_event(email="user@example.com")) is False

    def test_false_without_authorizer_context(self, whitelist: dict) -> None:
        assert admin_gate.is_admin(_event(authorizer=False)) is False

    def test_false_when_lookup_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(_: str) -> None:
            raise RuntimeError("supabase down")

        monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", _raise)
        assert admin_gate.is_admin(_event(email=ADMIN_EMAIL, groups="admin")) is False


class TestAdminEndpoint:
    """Through a real handler, with only the storage calls stubbed."""

    @pytest.fixture
    def writes(self, monkeypatch: pytest.MonkeyPatch, whitelist: dict) -> list:
        from lambdas.api_admin_users_update import handler as h

        calls: list = []
        monkeypatch.setattr(h, "get_row", lambda *a: {"sleeper_user_id": "u7", "is_admin": False})
        monkeypatch.setattr(h, "update_row", lambda *a, **k: calls.append(a) or {"is_admin": True})
        monkeypatch.setattr(h, "write_audit", lambda **k: None)
        return calls

    def _call(self, event: dict[str, Any]) -> tuple[int, dict]:
        from lambdas.api_admin_users_update.handler import handler

        resp = handler(event, None)
        return resp["statusCode"], json.loads(resp["body"])

    def _body(self) -> dict[str, Any]:
        return {"sleeper_user_id": ADMIN_SLEEPER_ID, "user_id": "u7", "fields": {"is_admin": True}}

    def test_non_admin_token_with_admin_sleeper_id_in_body_gets_403(self, writes: list) -> None:
        status, _ = self._call(_event(email="user@example.com", body=self._body()))
        assert status == 403
        assert writes == []

    def test_admin_token_gets_through(self, writes: list) -> None:
        status, _ = self._call(_event(email=ADMIN_EMAIL, groups="admin", body=self._body()))
        assert status == 200
        assert len(writes) == 1

    def test_missing_claims_is_rejected(self, writes: list) -> None:
        status, _ = self._call(_event(authorizer=False, body=self._body()))
        assert status in (401, 403)
        assert writes == []
