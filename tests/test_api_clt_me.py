"""
Tests for `lambdas.api_clt_me.handler` and the member gate behind it.

The gate is what keeps /clt/* inert for Xomper users, since xomper-client
tokens reach every route. Tables are moto-backed so the conditional sub
binding runs against real DynamoDB semantics.
"""
from __future__ import annotations

import importlib
import json

import boto3
import pytest
from moto import mock_aws

from lambdas.common.constants import CLT_MEMBERS_TABLE, PLATFORM_USERS_TABLE

MEMBER = {
    "email": "member@example.com",
    "displayName": "Member One",
    "role": "member",
    "sleeperUserId": "111",
    "active": True,
}


@pytest.fixture
def tables():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        members = dynamodb.create_table(
            TableName=CLT_MEMBERS_TABLE,
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        users = dynamodb.create_table(
            TableName=PLATFORM_USERS_TABLE,
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield members, users


@pytest.fixture
def mod(tables):
    from lambdas.common import clt_gate, platform_users

    importlib.reload(platform_users)
    importlib.reload(clt_gate)
    from lambdas.api_clt_me import handler as handler_mod

    return importlib.reload(handler_mod)


def event(sub="cog-1", email="member@example.com"):
    return {
        "httpMethod": "GET",
        "path": "/clt/me",
        "requestContext": {
            "authorizer": {"sub": sub, "email": email, "provider": "cognito", "groups": ""}
        },
    }


def body_of(response):
    return json.loads(response["body"])


def test_member_gets_their_row_and_linked_sleeper_id(mod, tables):
    members, users = tables
    members.put_item(Item=MEMBER)
    users.put_item(Item={"userId": "cog-1", "sleeperUserId": "222"})

    response = mod.handler(event(), None)

    assert response["statusCode"] == 200
    assert body_of(response) == {
        "member": {
            "email": "member@example.com",
            "displayName": "Member One",
            "role": "member",
            "sleeperUserId": "111",
        },
        "linkedSleeperUserId": "222",
    }


def test_member_without_a_platform_record_has_no_linked_id(mod, tables):
    members, _ = tables
    members.put_item(Item=MEMBER)

    response = mod.handler(event(), None)

    assert response["statusCode"] == 200
    assert body_of(response)["linkedSleeperUserId"] == ""


def test_email_match_ignores_case_and_whitespace(mod, tables):
    members, _ = tables
    members.put_item(Item=MEMBER)

    response = mod.handler(event(email="  Member@Example.COM "), None)

    assert response["statusCode"] == 200


def test_first_sign_in_binds_the_row_to_the_callers_sub(mod, tables):
    members, _ = tables
    members.put_item(Item=MEMBER)

    mod.handler(event(sub="cog-1"), None)

    assert members.get_item(Key={"email": "member@example.com"})["Item"]["sub"] == "cog-1"
    assert mod.handler(event(sub="cog-1"), None)["statusCode"] == 200


def test_another_sub_with_a_bound_members_email_is_refused(mod, tables):
    # The pool lets a user rewrite their own email claim, so a second
    # account presenting the address is not the member.
    members, _ = tables
    members.put_item(Item={**MEMBER, "sub": "cog-1"})

    response = mod.handler(event(sub="cog-intruder"), None)

    assert response["statusCode"] == 403
    assert members.get_item(Key={"email": "member@example.com"})["Item"]["sub"] == "cog-1"


def test_inactive_member_is_refused(mod, tables):
    members, _ = tables
    members.put_item(Item={**MEMBER, "active": False})

    response = mod.handler(event(), None)

    assert response["statusCode"] == 403
    assert "sub" not in members.get_item(Key={"email": "member@example.com"})["Item"]


def test_non_member_is_refused(mod, tables):
    members, _ = tables
    members.put_item(Item=MEMBER)

    response = mod.handler(event(sub="cog-2", email="stranger@example.com"), None)

    assert response["statusCode"] == 403
    assert body_of(response)["error"]["message"] == "not on the CLT roster"


def test_xomper_only_user_is_refused(mod, tables):
    # Signed in and Sleeper-linked through Xomper, but not on CLT's roster.
    _, users = tables
    users.put_item(Item={"userId": "cog-3", "email": "xomper@example.com", "sleeperUserId": "333"})

    response = mod.handler(event(sub="cog-3", email="xomper@example.com"), None)

    assert response["statusCode"] == 403


def test_caller_without_an_email_claim_is_refused(mod, tables):
    members, _ = tables
    members.put_item(Item=MEMBER)

    assert mod.handler(event(email=""), None)["statusCode"] == 403


def test_missing_authorizer_context_is_401(mod):
    bare = event()
    bare["requestContext"] = {}

    assert mod.handler(bare, None)["statusCode"] == 401
