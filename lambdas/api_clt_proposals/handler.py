"""
CLT rule proposals and votes. One Lambda, five routes:

    GET  /clt/proposals-list    every proposal for CLT's league, open first
    POST /clt/proposals-create  {"title": "...", "description": "..."}
    POST /clt/proposals-vote    {"proposalId": "...", "vote": "yes" | "no"}
    POST /clt/proposals-delete  {"proposalId": "..."}  proposer or admin
    POST /clt/proposals-status  {"proposalId": "...", "status": "..."}  admin

Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_member`; admin routes add Xomper's admin gate.

Members are keyed by email, so `proposed_by` and a vote's `user_id` hold the
member's email. Responses carry display names instead, never emails.

A vote is final: the conditional put refuses a second vote from the same
member rather than overwriting it.

Creating a proposal, and moving one to approved or rejected, emails active
members through `clt_alerts` while CLT's `emailNotifications` is on.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from lambdas.common import clt_alerts
from lambdas.common.clt_gate import list_members, require_clt_admin, require_member
from lambdas.common.constants import CLT_LEAGUE_ID, CLT_PROPOSALS_TABLE, CLT_VOTES_TABLE
from lambdas.common.errors import NotFoundError, ValidationError, XomperError, handle_errors
from lambdas.common.utility_helpers import parse_body, success_response

HANDLER = "api_clt_proposals"
TITLE_MAX = 120
DESCRIPTION_MAX = 2000
VOTES = ("yes", "no")
STATUSES = ("open", "approved", "rejected", "closed")


def _proposals() -> Any:
    return boto3.resource("dynamodb").Table(CLT_PROPOSALS_TABLE)


def _votes() -> Any:
    return boto3.resource("dynamodb").Table(CLT_VOTES_TABLE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _query_all(table: Any, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _votes_for(proposal_id: str) -> list[dict[str, Any]]:
    return _query_all(_votes(), KeyConditionExpression=Key("proposal_id").eq(proposal_id))


def _get(proposal_id: str, function: str) -> dict[str, Any]:
    proposal = _proposals().get_item(Key={"id": proposal_id}).get("Item")
    if not proposal or proposal.get("league_id") != CLT_LEAGUE_ID:
        raise NotFoundError("proposal not found", handler=HANDLER, function=function, resource=proposal_id)
    return proposal


def _names() -> dict[str, str]:
    return {m["email"]: m.get("displayName", "") for m in list_members()}


def _shape(
    proposal: dict[str, Any],
    votes: list[dict[str, Any]],
    names: dict[str, str],
    email: str,
) -> dict[str, Any]:
    voters = {v: [names.get(x["user_id"], "") for x in votes if x["vote"] == v] for v in VOTES}
    return {
        "id": proposal["id"],
        "title": proposal["title"],
        "description": proposal.get("description", ""),
        "status": proposal["status"],
        "proposedBy": names.get(proposal["proposed_by"], ""),
        "isMine": proposal["proposed_by"] == email,
        "createdAt": proposal["created_at"],
        "updatedAt": proposal.get("updated_at", proposal["created_at"]),
        "yesCount": len(voters["yes"]),
        "noCount": len(voters["no"]),
        "myVote": next((v["vote"] for v in votes if v["user_id"] == email), None),
        "voters": voters,
    }


def _text(body: dict[str, Any], field: str, max_len: int, required: bool) -> str:
    value = body.get(field, "")
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string", handler=HANDLER, field=field)
    value = value.strip()
    if (required and not value) or len(value) > max_len:
        low = 1 if required else 0
        raise ValidationError(f"{field} must be {low}-{max_len} characters", handler=HANDLER, field=field)
    return value


def _choice(body: dict[str, Any], field: str, options: tuple[str, ...]) -> str:
    value = body.get(field)
    if value not in options:
        raise ValidationError(f"{field} must be one of {', '.join(options)}", handler=HANDLER, field=field)
    return value


def _list(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    proposals = _query_all(
        _proposals(),
        IndexName="league_id-index",
        KeyConditionExpression=Key("league_id").eq(CLT_LEAGUE_ID),
    )
    names = _names()
    shaped = [_shape(p, _votes_for(p["id"]), names, member["email"]) for p in proposals]
    shaped.sort(key=lambda p: p["createdAt"], reverse=True)
    shaped.sort(key=lambda p: p["status"] != "open")
    return success_response({"proposals": shaped})


def _create(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    body = parse_body(event)
    now = _now()
    proposal = {
        "id": str(uuid.uuid4()),
        "league_id": CLT_LEAGUE_ID,
        "title": _text(body, "title", TITLE_MAX, required=True),
        "description": _text(body, "description", DESCRIPTION_MAX, required=False),
        "status": "open",
        "proposed_by": member["email"],
        "created_at": now,
        "updated_at": now,
    }
    _proposals().put_item(Item=proposal)
    clt_alerts.proposal_created(proposal, member.get("displayName", ""))
    names = {member["email"]: member.get("displayName", "")}
    return success_response({"proposal": _shape(proposal, [], names, member["email"])}, status_code=201)


def _vote(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    body = parse_body(event)
    vote = _choice(body, "vote", VOTES)
    proposal = _get(str(body.get("proposalId") or ""), "_vote")
    if proposal["status"] != "open":
        raise XomperError("voting on this proposal is closed", handler=HANDLER, function="_vote", status=409)

    try:
        _votes().put_item(
            Item={
                "proposal_id": proposal["id"],
                "user_id": member["email"],
                "vote": vote,
                "created_at": _now(),
            },
            ConditionExpression="attribute_not_exists(user_id)",
        )
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise XomperError("already voted on this proposal", handler=HANDLER, function="_vote", status=409) from err

    return success_response({"proposalId": proposal["id"], "vote": vote})


def _delete(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    proposal = _get(str(parse_body(event).get("proposalId") or ""), "_delete")
    if proposal["proposed_by"] != member["email"]:
        require_clt_admin(event)

    # Proposal first: if the vote cleanup then fails, the leftovers point at
    # nothing and never show, where the other order would leave a live
    # proposal with its votes half gone.
    _proposals().delete_item(Key={"id": proposal["id"]})
    with _votes().batch_writer() as batch:
        for v in _votes_for(proposal["id"]):
            batch.delete_item(Key={"proposal_id": v["proposal_id"], "user_id": v["user_id"]})

    return success_response({"deleted": proposal["id"]})


def _status(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    require_clt_admin(event)
    body = parse_body(event)
    status = _choice(body, "status", STATUSES)
    before = _get(str(body.get("proposalId") or ""), "_status")

    proposal = _proposals().update_item(
        Key={"id": before["id"]},
        UpdateExpression="SET #status = :status, updated_at = :now",
        # A delete landing between the read and this write must not leave
        # behind a new item holding only the status.
        ConditionExpression="attribute_exists(id)",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status, ":now": _now()},
        ReturnValues="ALL_NEW",
    )["Attributes"]

    names = _names()
    shaped = _shape(proposal, _votes_for(proposal["id"]), names, member["email"])
    if status != before["status"]:
        clt_alerts.proposal_decided(
            proposal,
            names.get(proposal["proposed_by"], ""),
            shaped["voters"]["yes"],
            shaped["voters"]["no"],
        )
    return success_response({"proposal": shaped})


Action = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

# Final path segment -> (action, expected method), as in api_users_me.
_ROUTES: dict[str, tuple[Action, str]] = {
    "proposals-list": (_list, "GET"),
    "proposals-create": (_create, "POST"),
    "proposals-vote": (_vote, "POST"),
    "proposals-delete": (_delete, "POST"),
    "proposals-status": (_status, "POST"),
}


@handle_errors(HANDLER)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    member = require_member(event)

    method = (event.get("httpMethod") or "GET").upper()
    path = event.get("path") or event.get("resource") or ""
    action, expected = _ROUTES.get(path.rstrip("/").rsplit("/", 1)[-1], (None, ""))
    if action is None or expected != method:
        raise ValidationError(f"Unsupported route: {method} {path}", handler=HANDLER)

    return action(event, member)
