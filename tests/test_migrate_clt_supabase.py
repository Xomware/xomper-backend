"""
Tests for `scripts/migrate_clt_supabase.py members`.

The fixture mirrors the Supabase `whitelisted_users` export shape with
synthetic people. The table is moto-backed so the write and the read-back
verification run against real DynamoDB semantics.
"""
from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from scripts import migrate_clt_supabase as loader


def row(n: int, **overrides):
    return {
        "id": f"00000000-0000-0000-0000-00000000000{n}",
        "email": f"member{n}@example.com",
        "display_name": f"Member {n}",
        "sleeper_user_id": f"10{n}",
        "sleeper_username": f"member{n}",
        "role": "member",
        "is_active": True,
        "is_admin": False,
        "notes": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        **overrides,
    }


ROWS = [
    row(1, role="owner", is_admin=True, email="  Member1@Example.com"),
    row(2),
    row(3, is_active=False),
]


@pytest.fixture
def export(tmp_path):
    def write(rows):
        path = tmp_path / "whitelisted_users.json"
        path.write_text(json.dumps(rows))
        return path

    return write


@pytest.fixture
def table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        yield dynamodb.create_table(
            TableName=loader.MEMBERS_TABLE,
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def test_dry_run_is_the_default_and_writes_nothing(export, table, capsys):
    assert loader.main(["members", str(export(ROWS))]) == 0

    assert table.scan()["Count"] == 0
    assert "export: 3 rows, 2 active, 1 admin" in capsys.readouterr().out


def test_write_loads_every_row_and_verifies(export, table, capsys):
    assert loader.main(["members", str(export(ROWS)), "--no-dry-run"]) == 0

    owner = table.get_item(Key={"email": "member1@example.com"})["Item"]
    assert owner == {
        "email": "member1@example.com",
        "id": "00000000-0000-0000-0000-000000000001",
        "displayName": "Member 1",
        "sleeperUserId": "101",
        "sleeperUsername": "member1",
        "role": "owner",
        "active": True,
        "isAdmin": True,
        "createdAt": "2026-01-01T00:00:00+00:00",
        "updatedAt": "2026-01-02T00:00:00+00:00",
    }
    assert table.get_item(Key={"email": "member3@example.com"})["Item"]["active"] is False
    out = capsys.readouterr().out
    assert "table: 3 rows, 2 active, 1 admin" in out
    assert "verification passed" in out


def test_rerun_overwrites_fields_but_keeps_the_bound_sub(export, table):
    loader.main(["members", str(export(ROWS)), "--no-dry-run"])
    table.update_item(
        Key={"email": "member2@example.com"},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "sub"},
        ExpressionAttributeValues={":s": "cog-2"},
    )

    renamed = [ROWS[0], row(2, display_name="Renamed"), ROWS[2]]
    assert loader.main(["members", str(export(renamed)), "--no-dry-run"]) == 0

    item = table.get_item(Key={"email": "member2@example.com"})["Item"]
    assert item["sub"] == "cog-2"
    assert item["displayName"] == "Renamed"
    assert table.scan()["Count"] == 3


def test_verification_fails_on_rows_not_in_the_export(export, table, capsys):
    table.put_item(Item={"email": "stray@example.com", "active": True})

    assert loader.main(["members", str(export(ROWS)), "--no-dry-run"]) == 1

    out = capsys.readouterr().out
    assert "1 table rows are not in the export" in out
    assert "verification FAILED" in out


def test_duplicate_emails_fail_before_anything_is_written(export, table, capsys):
    rows = ROWS + [row(4, email="MEMBER2@example.com")]

    assert loader.main(["members", str(export(rows)), "--no-dry-run"]) == 1

    assert table.scan()["Count"] == 0
    assert "nothing written" in capsys.readouterr().out


def test_output_never_contains_an_email(export, table, capsys):
    rows = ROWS + [row(4, email="MEMBER2@example.com")]
    loader.main(["members", str(export(rows)), "--no-dry-run"])
    table.put_item(Item={"email": "stray@example.com", "active": True})
    loader.main(["members", str(export(ROWS)), "--no-dry-run"])

    assert "@" not in capsys.readouterr().out
