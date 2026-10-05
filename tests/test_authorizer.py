"""
Tests for `lambdas.authorizer.handler`.

The authorizer verifies Cognito RS256 tokens. Tokens here are really signed
and really verified — the JWKS *fetch* is stubbed, the signature check is not,
so a change that weakens verification fails these rather than passing on a
mock.

The case that matters most is the last one: the shared `xomware-users` pool
issues tokens for every Xomware app, so a valid Cognito token is not by itself
a token for Xomper.
"""
from __future__ import annotations

import importlib
from fnmatch import fnmatchcase

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

POOL_ID = "us-east-1_ZrN8NaaIv"
CLIENT_ID = "38e5sjavoa76ghbl5hpjsapc49"
CLT_CLIENT_ID = "481dfniqtuh8062gjqr1o1u469"
REGION = "us-east-1"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
SUPABASE_URL = "https://proj.supabase.co"
METHOD_ARN = (
    "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/GET/users/me"
)

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def authorizer(monkeypatch):
    """Load the authorizer with Cognito configured.

    The PyJWKClient is replaced with a stub that returns a fixed key, standing
    in for the JWKS endpoint. Everything downstream of the key lookup —
    signature, expiry, issuer, client — runs for real.
    """
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("COGNITO_CLT_CLIENT_ID", CLT_CLIENT_ID)
    monkeypatch.setenv("AWS_REGION", REGION)

    from lambdas.authorizer import handler as mod

    mod = importlib.reload(mod)

    class Stub:
        def __init__(self, key):
            self.key = key

        def get_signing_key_from_jwt(self, _token):
            return self

    monkeypatch.setattr(mod, "_cognito_jwks", Stub(RSA_KEY.public_key()))
    return mod


def cognito_token(key=RSA_KEY, **overrides):
    claims = {
        "sub": "cog-user-1",
        "email": "d@x.com",
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "token_use": "id",
        "exp": 4102444800,  # 2100-01-01
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def supabase_token(**overrides):
    claims = {
        "sub": "sb-user-1",
        "email": "d@x.com",
        "aud": "authenticated",
        "exp": 4102444800,
        **overrides,
    }
    return jwt.encode(claims, EC_KEY, algorithm="ES256")


def effect(policy):
    return policy["policyDocument"]["Statement"][0]["Effect"]


def test_allows_a_cognito_token(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token()}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Allow"
    assert policy["context"]["provider"] == "cognito"
    assert policy["context"]["sub"] == "cog-user-1"


def test_denies_a_supabase_token(authorizer):
    # The dual-provider window is closed. A Supabase token is now just a
    # token signed by a key this pool does not publish. xomper-ios still
    # sends one (Xomware/xomper-ios#166) and gets a 403 until it migrates.
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {supabase_token()}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Deny"


def test_passes_cognito_groups_through_for_admin_checks(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token(**{'cognito:groups': ['admin']})}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert policy["context"]["groups"] == "admin"


def test_denies_a_token_signed_by_the_wrong_key(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token(key=OTHER_RSA_KEY)}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Deny"


def test_denies_an_expired_token(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token(exp=1000000000)}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Deny"


def test_denies_a_wrong_issuer(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token(iss='https://evil.example')}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Deny"


def test_denies_a_missing_token(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": "", "methodArn": METHOD_ARN}, None
    )

    assert effect(policy) == "Deny"


def test_denies_a_token_for_another_app_on_the_shared_pool(authorizer):
    # xomforms and xomtracks sign into the same pool. Their tokens are
    # correctly signed by the same keys and carry the same issuer — the app
    # client is the only thing separating them.
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token(aud='82sn0drkf2fvfjn94nmoc857p')}",
         "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Deny"


def test_accepts_an_access_token_which_carries_client_id_not_aud(authorizer):
    token = cognito_token(token_use="access", client_id=CLIENT_ID)
    stripped = jwt.encode(
        {k: v for k, v in jwt.decode(token, options={"verify_signature": False}).items()
         if k != "aud"},
        RSA_KEY,
        algorithm="RS256",
    )

    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {stripped}", "methodArn": METHOD_ARN},
        None,
    )

    assert effect(policy) == "Allow"


def test_allow_is_scoped_to_the_api_stage_not_one_method(authorizer):
    policy = authorizer.handler(
        {"authorizationToken": f"Bearer {cognito_token()}",
         "methodArn": METHOD_ARN},
        None,
    )

    resource = policy["policyDocument"]["Statement"][0]["Resource"]
    # Wildcarding the stage is what makes the decision cacheable across
    # routes; without it every endpoint pays a fresh authorizer invocation.
    assert resource.endswith("abc123/dev/*")


STAGE_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/dev"


def allows(policy, method_arn):
    """Evaluate the policy the way API Gateway does against a cached decision.

    IAM's `*` matches any run of characters, slashes included, which is what
    fnmatchcase does for ARNs (they contain no `[`).
    """
    statement = policy["policyDocument"]["Statement"][0]
    resources = statement["Resource"]
    if isinstance(resources, str):
        resources = [resources]
    return statement["Effect"] == "Allow" and any(
        fnmatchcase(method_arn, r) for r in resources
    )


def authorize(authorizer, token, route):
    return authorizer.handler(
        {"authorizationToken": f"Bearer {token}", "methodArn": f"{STAGE_ARN}/{route}"},
        None,
    )


def test_xomper_client_policy_is_unchanged(authorizer):
    policy = authorize(authorizer, cognito_token(), "GET/users/me")

    assert policy == {
        "principalId": "cog-user-1",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:*",
                    "Effect": "Allow",
                    "Resource": f"{STAGE_ARN}/*",
                }
            ],
        },
        "context": {
            "sub": "cog-user-1",
            "email": "d@x.com",
            "provider": "cognito",
            "groups": "",
        },
    }


def test_clt_token_policy_covers_other_listed_routes_from_cache(authorizer):
    # One decision, cached per token, has to serve every CLT route.
    policy = authorize(authorizer, cognito_token(aud=CLT_CLIENT_ID), "GET/clt/me")

    assert allows(policy, f"{STAGE_ARN}/GET/clt/me")
    assert allows(policy, f"{STAGE_ARN}/GET/ai-reports/latest")
    assert allows(policy, f"{STAGE_ARN}/POST/clt/proposals-vote")
    assert policy["context"]["sub"] == "cog-user-1"


def test_clt_access_token_is_recognised_by_client_id(authorizer):
    token = cognito_token(token_use="access", client_id=CLT_CLIENT_ID, aud=None)

    policy = authorize(authorizer, token, "GET/players/list")

    assert allows(policy, f"{STAGE_ARN}/GET/players/list")
    assert not allows(policy, f"{STAGE_ARN}/GET/me/leagues")


@pytest.mark.parametrize("route", [
    "GET/me/leagues",
    "PUT/me/display-name",
    "POST/admin/users-update",
    "POST/values/compute",
    "POST/me/profile",
])
def test_clt_token_is_denied_on_unlisted_xomper_routes(authorizer, route):
    policy = authorize(authorizer, cognito_token(aud=CLT_CLIENT_ID), route)

    assert not allows(policy, f"{STAGE_ARN}/{route}")


def test_foreign_client_is_denied_with_clt_configured(authorizer):
    policy = authorize(authorizer, cognito_token(aud="82sn0drkf2fvfjn94nmoc857p"), "GET/clt/me")

    assert effect(policy) == "Deny"
