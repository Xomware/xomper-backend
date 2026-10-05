"""Shared moto tables and events for the /clt/* handler tests."""
from __future__ import annotations

import json
from typing import Any

import boto3

from lambdas.common.constants import (
    CLT_MEMBERS_TABLE,
    CLT_PROPOSALS_TABLE,
    CLT_SETTINGS_TABLE,
    CLT_TAXI_TABLE,
    CLT_VOTES_TABLE,
)

ADMIN_GROUPS = "us-east-1_x_Google,admin"


def create_table(name: str, hash_key: str, range_key: str = "", gsi: str = "") -> Any:
    keys = [(hash_key, "HASH")] + ([(range_key, "RANGE")] if range_key else [])
    attributes = {k for k, _ in keys} | ({gsi} if gsi else set())
    spec: dict[str, Any] = {
        "TableName": name,
        "KeySchema": [{"AttributeName": k, "KeyType": t} for k, t in keys],
        "AttributeDefinitions": [{"AttributeName": a, "AttributeType": "S"} for a in attributes],
        "BillingMode": "PAY_PER_REQUEST",
    }
    if gsi:
        spec["GlobalSecondaryIndexes"] = [{
            "IndexName": f"{gsi}-index",
            "KeySchema": [{"AttributeName": gsi, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }]
    return boto3.resource("dynamodb", region_name="us-east-1").create_table(**spec)


def create_members(*rows: dict[str, Any]) -> Any:
    table = create_table(CLT_MEMBERS_TABLE, "email")
    for row in rows:
        table.put_item(Item=row)
    return table


def create_proposal_tables() -> tuple[Any, Any]:
    return (
        create_table(CLT_PROPOSALS_TABLE, "id", gsi="league_id"),
        create_table(CLT_VOTES_TABLE, "proposal_id", "user_id"),
    )


def create_taxi_table() -> Any:
    return create_table(CLT_TAXI_TABLE, "league_id", "player_id")


def create_settings_table() -> Any:
    return create_table(CLT_SETTINGS_TABLE, "id")


def member(n: int, **overrides: Any) -> dict[str, Any]:
    """Member n: email m{n}@example.com, bound to sub cog-{n}."""
    return {
        "email": f"m{n}@example.com",
        "displayName": f"Member {n}",
        "role": "member",
        "sleeperUserId": f"u{n}",
        "active": True,
        "sub": f"cog-{n}",
        **overrides,
    }


def event(
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    n: int = 1,
    groups: str = "",
) -> dict[str, Any]:
    return {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "sub": f"cog-{n}",
                "email": f"m{n}@example.com",
                "provider": "cognito",
                "groups": groups,
            }
        },
    }


def body_of(response: dict[str, Any]) -> Any:
    return json.loads(response["body"])
