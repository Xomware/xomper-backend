"""
Tests for per-request CORS origin echo in `handle_errors`.

The decorator is the one place every API Lambda's response passes through,
so these drive real decorated handlers built from the real response helpers.
The Xomper cases compare whole responses: its behaviour must not change.
"""
from __future__ import annotations

import json

import pytest

from lambdas.common.errors import NotFoundError, handle_errors
from lambdas.common.utility_helpers import CORS_HEADERS, success_response

XOMPER = "https://xomper.xomware.com"
CLT = "https://clt.dynasty.xomware.com"


@handle_errors("test_ok")
def ok_handler(event, context):
    return success_response({"ok": True})


@handle_errors("test_raises")
def raising_handler(event, context):
    raise NotFoundError("nope", handler="test_raises")


@handle_errors("test_crashes")
def crashing_handler(event, context):
    raise RuntimeError("boom")


@handle_errors("test_cron")
def cron_handler(event, context):
    return success_response({"ok": True}, is_api=False)


def event(headers=None, multi=None):
    return {"httpMethod": "GET", "path": "/x", "headers": headers, "multiValueHeaders": multi}


def baseline(handler):
    return handler(event(), None)


@pytest.mark.parametrize("handler", [ok_handler, raising_handler, crashing_handler])
def test_xomper_origin_gets_the_response_unchanged(handler):
    response = handler(event({"Origin": XOMPER}), None)

    assert response == baseline(handler)
    assert response["headers"]["Access-Control-Allow-Origin"] == XOMPER
    assert "Vary" not in response["headers"]


def test_clt_origin_is_echoed_on_success():
    response = ok_handler(event({"Origin": CLT}), None)

    assert response["headers"]["Access-Control-Allow-Origin"] == CLT
    assert response["headers"]["Vary"] == "Origin"
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}


@pytest.mark.parametrize("handler,status", [(raising_handler, 404), (crashing_handler, 500)])
def test_clt_origin_is_echoed_on_errors(handler, status):
    response = handler(event({"Origin": CLT}), None)

    assert response["statusCode"] == status
    assert response["headers"]["Access-Control-Allow-Origin"] == CLT
    assert response["headers"]["Vary"] == "Origin"


@pytest.mark.parametrize("name", ["origin", "ORIGIN", "Origin"])
def test_header_name_is_matched_case_insensitively(name):
    response = ok_handler(event({name: CLT}), None)

    assert response["headers"]["Access-Control-Allow-Origin"] == CLT


def test_origin_is_read_from_multi_value_headers():
    response = ok_handler(event(headers=None, multi={"origin": [CLT]}), None)

    assert response["headers"]["Access-Control-Allow-Origin"] == CLT


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "https://clt.dynasty.xomware.com.evil.example",
    "http://clt.dynasty.xomware.com",
    "",
])
def test_unlisted_origin_gets_the_xomper_default(origin):
    response = ok_handler(event({"Origin": origin}), None)

    assert response == baseline(ok_handler)


@pytest.mark.parametrize("evt", [{}, {"headers": None}, {"headers": {}}, {"multiValueHeaders": {"origin": []}}])
def test_missing_headers_get_the_xomper_default(evt):
    assert ok_handler(evt, None) == baseline(ok_handler)


def test_shared_header_dict_is_never_mutated():
    # success_response hands out the module-level CORS_HEADERS dict, so an
    # in-place edit would leak CLT's origin into later warm invocations.
    ok_handler(event({"Origin": CLT}), None)

    assert CORS_HEADERS["Access-Control-Allow-Origin"] == XOMPER
    assert "Vary" not in CORS_HEADERS
    assert ok_handler(event(), None)["headers"]["Access-Control-Allow-Origin"] == XOMPER


def test_cron_responses_pass_through():
    assert cron_handler({"source": "aws.events"}, None) == baseline(cron_handler)


def test_a_real_handler_echoes_clt_on_its_401():
    from lambdas.api_users_me.handler import handler

    response = handler({"httpMethod": "GET", "path": "/me/profile",
                        "headers": {"origin": CLT}, "requestContext": {}}, None)

    assert response["statusCode"] == 401
    assert response["headers"]["Access-Control-Allow-Origin"] == CLT
