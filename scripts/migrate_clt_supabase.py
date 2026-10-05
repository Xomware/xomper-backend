#!/usr/bin/env python3
"""
CLT Supabase loader
===================
Copies CLT's Supabase export into Xomper's DynamoDB tables, one subcommand
per entity. Only `members` exists so far.

    python3 scripts/migrate_clt_supabase.py members <export>/whitelisted_users.json
    python3 scripts/migrate_clt_supabase.py members <export>/whitelisted_users.json --no-dry-run

The default dry run validates and transforms the export without touching AWS.
--no-dry-run writes with your AWS credentials, re-reads the whole table, and
exits 1 if it doesn't match the export.

Re-runs are safe. Rows are keyed by email and written with update_item, so a
re-run overwrites the loaded fields and leaves `sub` alone. clt_gate stamps
`sub` at a member's first sign-in, and losing it would let the next account
presenting that email claim the row.

Output carries counts and Supabase ids, never emails: the export holds member
emails and this repo is public.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import boto3

# CLT_MEMBERS_TABLE in lambdas/common/constants.py. Not imported: constants
# requires Lambda env vars at import time.
MEMBERS_TABLE = "xomper-whitelisted-users"


def member_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        item = {
            # Same normalisation as clt_gate.normalize_email.
            "email": row["email"].strip().lower(),
            "id": row["id"],
            "displayName": row["display_name"] or "",
            "sleeperUserId": row["sleeper_user_id"] or "",
            "sleeperUsername": row["sleeper_username"] or "",
            "role": row["role"] or "",
            "active": bool(row["is_active"]),
            "isAdmin": bool(row["is_admin"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if row.get("notes"):
            item["notes"] = row["notes"]
        items.append(item)
    return items


def validate(items: list[dict[str, Any]]) -> list[str]:
    problems = [f"id={i['id']}: empty email" for i in items if not i["email"]]
    counts = Counter(i["email"] for i in items)
    problems += [
        f"id={i['id']}: email shared with another row"
        for i in items
        if i["email"] and counts[i["email"]] > 1
    ]
    return problems


def write(table: Any, items: list[dict[str, Any]]) -> None:
    for item in items:
        fields = {k: v for k, v in item.items() if k != "email"}
        table.update_item(
            Key={"email": item["email"]},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames={f"#{k}": k for k in fields},
            ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
        )


def scan_all(table: Any) -> list[dict[str, Any]]:
    response = table.scan()
    rows = response["Items"]
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        rows += response["Items"]
    return rows


def verify(items: list[dict[str, Any]], stored: list[dict[str, Any]]) -> list[str]:
    by_email = {row["email"]: row for row in stored}
    problems = []
    for item in items:
        row = by_email.get(item["email"])
        if row is None:
            problems.append(f"id={item['id']}: not in the table")
            continue
        problems += [
            f"id={item['id']}: {k} differs" for k, v in item.items() if row.get(k) != v
        ]

    extra = len(set(by_email) - {i["email"] for i in items})
    if extra:
        problems.append(f"{extra} table rows are not in the export")
    return problems


def summary(label: str, rows: list[dict[str, Any]]) -> str:
    active = sum(1 for r in rows if r.get("active"))
    admins = sum(1 for r in rows if r.get("isAdmin"))
    return f"{label}: {len(rows)} rows, {active} active, {admins} admin"


def members(path: Path, dry_run: bool) -> int:
    items = member_items(json.loads(path.read_text()))
    print(summary("export", items))

    problems = validate(items)
    if problems:
        print("\n".join(problems))
        print("export is invalid; nothing written")
        return 1

    if dry_run:
        print(f"dry run: nothing written. Re-run with --no-dry-run to write {MEMBERS_TABLE}.")
        return 0

    table = boto3.resource("dynamodb").Table(MEMBERS_TABLE)
    write(table, items)
    print(f"wrote {len(items)} rows to {MEMBERS_TABLE}")

    stored = scan_all(table)
    print(summary("table", stored))
    problems = verify(items, stored)
    if problems:
        print("\n".join(problems))
        print("verification FAILED")
        return 1
    print("verification passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    entities = parser.add_subparsers(dest="entity", required=True)

    members_cmd = entities.add_parser("members", help="whitelisted_users -> " + MEMBERS_TABLE)
    members_cmd.add_argument("path", type=Path, help="whitelisted_users.json from the export")
    members_cmd.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)

    args = parser.parse_args(argv)
    return members(args.path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
