"""
CLT league settings: one item in `CLT_SETTINGS_TABLE`.

Keyed by a constant rather than the season's league id, so it survives
Sleeper's yearly renewal. A missing item reads as every setting off, which is
why Terraform creates no item.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import boto3

from lambdas.common.constants import CLT_SETTINGS_TABLE

SETTINGS_ID = "clt"


def _table() -> Any:
    return boto3.resource("dynamodb").Table(CLT_SETTINGS_TABLE)


def email_notifications_on() -> bool:
    item = _table().get_item(Key={"id": SETTINGS_ID}).get("Item") or {}
    return bool(item.get("emailNotifications"))


def set_email_notifications(on: bool) -> None:
    _table().update_item(
        Key={"id": SETTINGS_ID},
        UpdateExpression="SET emailNotifications = :on, updatedAt = :now",
        ExpressionAttributeValues={":on": on, ":now": datetime.now(timezone.utc).isoformat()},
    )
