"""
CLT's own emails: new proposal, proposal decided, steal request.

Sent only while the CLT `emailNotifications` setting is on, to active
members, with links to the CLT site. The templates and SES path are Xomper's,
so the emails carry Xomper's branding (plan open question 1).

Every alert follows a write that has already committed, so a failure here is
logged and never raised: a 500 would invite the client to retry and
duplicate the proposal or request.
"""
from __future__ import annotations

from typing import Any, Callable

import boto3

from lambdas.common.clt_gate import list_members
from lambdas.common.clt_settings import email_notifications_on
from lambdas.common.constants import CLT_URL, PLAYERS_TABLE_NAME
from lambdas.common.email_templates import (
    generate_rule_accepted_email,
    generate_rule_accepted_email_plain_text,
    generate_rule_denied_email,
    generate_rule_denied_email_plain_text,
    generate_rule_proposed_email,
    generate_rule_proposed_email_plain_text,
    generate_taxi_steal_league_email,
    generate_taxi_steal_league_email_plain_text,
    generate_taxi_steal_owner_email,
    generate_taxi_steal_owner_email_plain_text,
)
from lambdas.common.errors import mask_sensitive_data
from lambdas.common.logger import get_logger
from lambdas.common.ses_helper import send_emails_concurrently

log = get_logger(__file__)

LEAGUE_NAME = "CLT Dynasty League"
Task = tuple[str, str, str, str]


def _deliver(template: str, build: Callable[[list[dict[str, Any]]], list[Task]]) -> None:
    try:
        if not email_notifications_on():
            return
        active = [m for m in list_members() if m.get("active")]
        sent, failed = send_emails_concurrently(build(active), template=template)
        log.info(f"clt_alerts: {template} sent={sent} failed={failed}")
    except Exception as err:  # noqa: BLE001 — see module docstring
        log.error(f"clt_alerts: {template} not sent: {type(err).__name__}: {mask_sensitive_data(str(err))}")


def proposal_created(proposal: dict[str, Any], proposer_name: str) -> None:
    def build(active: list[dict[str, Any]]) -> list[Task]:
        args = {
            "proposer_name": proposer_name,
            "rule_title": proposal["title"],
            "rule_description": proposal.get("description", ""),
            "vote_url": CLT_URL,
            "league_name": LEAGUE_NAME,
        }
        html = generate_rule_proposed_email(**args)
        text = generate_rule_proposed_email_plain_text(**args)
        subject = f"New Rule Proposal: {proposal['title']}"
        return [(m["email"], subject, html, text) for m in active]

    _deliver("clt_rule_proposed", build)


def proposal_decided(
    proposal: dict[str, Any],
    proposer_name: str,
    approved_by: list[str],
    rejected_by: list[str],
) -> None:
    """Approved or rejected only; other status changes send nothing."""
    if proposal["status"] == "approved":
        html_fn, text_fn, label = generate_rule_accepted_email, generate_rule_accepted_email_plain_text, "APPROVED"
    elif proposal["status"] == "rejected":
        html_fn, text_fn, label = generate_rule_denied_email, generate_rule_denied_email_plain_text, "DENIED"
    else:
        return

    def build(active: list[dict[str, Any]]) -> list[Task]:
        args = {
            "proposer_name": proposer_name,
            "rule_title": proposal["title"],
            "rule_description": proposal.get("description", ""),
            "approved_voters": approved_by,
            "rejected_voters": rejected_by,
            "league_url": CLT_URL,
            "league_name": LEAGUE_NAME,
        }
        subject = f"Rule {label}: {proposal['title']}"
        return [(m["email"], subject, html_fn(**args), text_fn(**args)) for m in active]

    _deliver(f"clt_rule_{proposal['status']}", build)


def steal_requested(player_id: str, stealer_name: str, owner_sleeper_id: str) -> None:
    """League-wide alert to active members, plus the owner's own warning."""

    def build(active: list[dict[str, Any]]) -> list[Task]:
        player = boto3.resource("dynamodb").Table(PLAYERS_TABLE_NAME).get_item(
            Key={"playerId": player_id}
        ).get("Item") or {}
        owner = next((m for m in active if m.get("sleeperUserId") == owner_sleeper_id), None)
        args = {
            "stealer_name": stealer_name,
            "player_name": player.get("full_name") or f"Player {player_id}",
            "player_position": player.get("position") or "N/A",
            "player_team": player.get("team") or "N/A",
            "league_url": CLT_URL,
            "league_name": LEAGUE_NAME,
        }
        owner_name = owner.get("displayName", "") if owner else ""
        subject = f"Taxi Squad Alert: {stealer_name} is stealing {args['player_name']}!"
        html = generate_taxi_steal_league_email(target_owner_name=owner_name, **args)
        text = generate_taxi_steal_league_email_plain_text(target_owner_name=owner_name, **args)
        tasks = [(m["email"], subject, html, text) for m in active if m is not owner]
        if owner:
            subject = f"URGENT: {stealer_name} is stealing {args['player_name']} from your taxi squad!"
            html = generate_taxi_steal_owner_email(owner_name=owner_name, **args)
            text = generate_taxi_steal_owner_email_plain_text(owner_name=owner_name, **args)
            tasks.append((owner["email"], subject, html, text))
        return tasks

    _deliver("clt_taxi_steal", build)
