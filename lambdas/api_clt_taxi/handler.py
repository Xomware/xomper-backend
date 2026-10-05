"""
CLT taxi squad steal requests. One Lambda, two routes:

    GET  /clt/taxi-list     every steal request in CLT's current league
    POST /clt/taxi-request  {"playerId": "<sleeper player id>"}

Caller identity comes from `requestContext.authorizer` through
`clt_gate.require_member`.

The player must sit on another roster's taxi squad in CLT's league right now,
checked against Sleeper. The table is keyed by league + player, so a player
can carry one request; a second gets 409. `requested_by` holds the member's
email, and responses carry display names instead.

A new request emails active members, and the roster's owner separately,
through `clt_alerts` while CLT's `emailNotifications` is on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from lambdas.common import clt_alerts
from lambdas.common.clt_gate import list_members, require_member
from lambdas.common.constants import CLT_LEAGUE_ID, CLT_TAXI_TABLE
from lambdas.common.errors import ValidationError, XomperError, handle_errors
from lambdas.common.sleeper_helper import get_sleeper_league_rosters
from lambdas.common.utility_helpers import parse_body, success_response

HANDLER = "api_clt_taxi"


def _table() -> Any:
    return boto3.resource("dynamodb").Table(CLT_TAXI_TABLE)


def _shape(item: dict[str, Any], names: dict[str, str], email: str) -> dict[str, Any]:
    return {
        "playerId": item["player_id"],
        "rosterId": item["roster_id"],
        "requestedBy": names.get(item["requested_by"], ""),
        "isMine": item["requested_by"] == email,
        "createdAt": item["created_at"],
    }


def _list(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("league_id").eq(CLT_LEAGUE_ID)}
    while True:
        page = _table().query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    names = {m["email"]: m.get("displayName", "") for m in list_members()}
    requests = sorted(
        (_shape(i, names, member["email"]) for i in items),
        key=lambda r: r["createdAt"],
        reverse=True,
    )
    return success_response({"leagueId": CLT_LEAGUE_ID, "requests": requests})


def _request(event: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    player_id = parse_body(event).get("playerId")
    if not isinstance(player_id, str) or not player_id.strip():
        raise ValidationError("playerId is required", handler=HANDLER, function="_request", field="playerId")
    player_id = player_id.strip()

    rosters = get_sleeper_league_rosters(CLT_LEAGUE_ID) or []
    roster = next((r for r in rosters if player_id in (r.get("taxi") or [])), None)
    if roster is None:
        raise ValidationError("player is not on a taxi squad", handler=HANDLER, function="_request", field="playerId")

    owners = {roster.get("owner_id"), *(roster.get("co_owners") or [])}
    if member.get("sleeperUserId") and member["sleeperUserId"] in owners:
        raise ValidationError("player is on your own taxi squad", handler=HANDLER, function="_request", field="playerId")

    item = {
        "league_id": CLT_LEAGUE_ID,
        "player_id": player_id,
        "roster_id": roster["roster_id"],
        "owner_id": roster.get("owner_id") or "",
        "requested_by": member["email"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(player_id)")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise XomperError(
            "this player already has a steal request",
            handler=HANDLER,
            function="_request",
            status=409,
        ) from err

    clt_alerts.steal_requested(player_id, member.get("displayName", ""), item["owner_id"])
    names = {member["email"]: member.get("displayName", "")}
    return success_response({"request": _shape(item, names, member["email"])}, status_code=201)


_ROUTES = {
    "taxi-list": (_list, "GET"),
    "taxi-request": (_request, "POST"),
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
