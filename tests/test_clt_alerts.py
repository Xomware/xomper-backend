"""
CLT email alerts, exercised through the proposal and taxi handlers so the
trigger points are tested along with the recipients. SES is replaced at
`clt_alerts.send_emails_concurrently`; everything else is moto.
"""
from __future__ import annotations

import pytest
from moto import mock_aws

from lambdas.common import admin_gate, clt_alerts, clt_settings
from lambdas.common.constants import CLT_URL, PLAYERS_TABLE_NAME
from tests.clt_support import (
    ADMIN_GROUPS,
    body_of,
    create_members,
    create_proposal_tables,
    create_settings_table,
    create_table,
    create_taxi_table,
    event,
    member,
)

ADMIN = 9
ACTIVE = ["m1@example.com", "m2@example.com", "m3@example.com", "m9@example.com"]
ROSTERS = [{"roster_id": 2, "owner_id": "u2", "taxi": ["p-rookie"]}]


@pytest.fixture
def sent(monkeypatch):
    admins = {"m9@example.com": {"email": "m9@example.com", "is_admin": True}}
    monkeypatch.setattr(admin_gate, "get_whitelisted_user_by_email", admins.get)
    batches = []

    def fake_send(tasks, template=None):
        batches.append((template, tasks))
        return len(tasks), 0

    monkeypatch.setattr(clt_alerts, "send_emails_concurrently", fake_send)
    with mock_aws():
        create_members(member(1), member(2), member(3), member(4, active=False), member(ADMIN))
        create_proposal_tables()
        create_taxi_table()
        create_settings_table()
        players = create_table(PLAYERS_TABLE_NAME, "playerId")
        players.put_item(Item={"playerId": "p-rookie", "full_name": "Rookie Back", "position": "RB", "team": "NYG"})
        from lambdas.api_clt_taxi import handler as taxi

        monkeypatch.setattr(taxi, "get_sleeper_league_rosters", lambda _id: ROSTERS)
        yield batches


def proposals(route, body, n=1, admin=False):
    from lambdas.api_clt_proposals.handler import handler

    method = "GET" if route == "list" else "POST"
    return handler(event(f"/clt/proposals-{route}", method, body, n=n, groups=ADMIN_GROUPS if admin else ""), None)


def steal(n=1):
    from lambdas.api_clt_taxi.handler import handler

    return handler(event("/clt/taxi-request", "POST", {"playerId": "p-rookie"}, n=n), None)


def recipients(tasks):
    return sorted(t[0] for t in tasks)


def test_nothing_is_sent_while_the_setting_is_off(sent):
    proposals("create", {"title": "Two IR slots"})
    steal()

    assert sent == []


def test_new_proposal_emails_every_active_member(sent):
    clt_settings.set_email_notifications(True)

    assert proposals("create", {"title": "Two IR slots"})["statusCode"] == 201

    [(template, tasks)] = sent
    assert template == "clt_rule_proposed"
    assert recipients(tasks) == ACTIVE
    _, subject, html, text = tasks[0]
    assert subject == "New Rule Proposal: Two IR slots"
    assert CLT_URL in html
    assert "Member 1" in text


def test_only_a_change_to_approved_or_rejected_emails(sent):
    proposal = body_of(proposals("create", {"title": "Two IR slots"}))["proposal"]
    proposals("vote", {"proposalId": proposal["id"], "vote": "yes"}, n=2)
    clt_settings.set_email_notifications(True)

    def set_status(status):
        proposals("status", {"proposalId": proposal["id"], "status": status}, n=ADMIN, admin=True)

    set_status("approved")
    set_status("approved")
    set_status("closed")
    set_status("rejected")

    assert [template for template, _ in sent] == ["clt_rule_approved", "clt_rule_rejected"]
    _, approved = sent[0]
    assert recipients(approved) == ACTIVE
    assert approved[0][1] == "Rule APPROVED: Two IR slots"
    assert "Member 2" in approved[0][3]


def test_steal_request_warns_the_owner_and_tells_everyone_else(sent):
    clt_settings.set_email_notifications(True)

    assert steal(n=1)["statusCode"] == 201

    [(template, tasks)] = sent
    assert template == "clt_taxi_steal"
    league = [t for t in tasks if t[1].startswith("Taxi Squad Alert")]
    owner = [t for t in tasks if t[1].startswith("URGENT")]
    assert recipients(league) == ["m1@example.com", "m3@example.com", "m9@example.com"]
    assert recipients(owner) == ["m2@example.com"]
    assert owner[0][1] == "URGENT: Member 1 is stealing Rookie Back from your taxi squad!"


def test_a_failed_send_still_returns_the_committed_write(sent, monkeypatch):
    clt_settings.set_email_notifications(True)

    def boom(tasks, template=None):
        raise RuntimeError("SES throttled")

    monkeypatch.setattr(clt_alerts, "send_emails_concurrently", boom)

    response = proposals("create", {"title": "Two IR slots"})

    assert response["statusCode"] == 201
    assert len(body_of(proposals("list", None))["proposals"]) == 1
