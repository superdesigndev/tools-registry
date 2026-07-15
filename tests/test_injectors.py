"""The four auth shapes, unit-tested directly on the seam (no DB, no network).

A binding is a plain dict: {injector, location, name, format, secret_field, secret_id}.
env / cli_auth place a string; secret_file / oauth pull a field from a JSON blob.
"""

from __future__ import annotations

import pytest

from treg.injectors import inject


def _b(**kw) -> dict:
    base = {
        "injector": "env",
        "location": "header",
        "name": "Authorization",
        "format": "Bearer {secret}",
        "secret_field": "access_token",
    }
    base.update(kw)
    return base


def test_env_header():
    h: dict[str, str] = {}
    inject(h, [], _b(), "ABC")
    assert h["Authorization"] == "Bearer ABC"


def test_env_query_appends_pair():
    p: list = []
    inject({}, p, _b(location="query", name="api_key", format="{secret}"), "ABC")
    assert p == [("api_key", "ABC")]


def test_query_injection_overrides_caller_param_of_same_name():
    p: list = [("api_key", "caller"), ("keep", "me")]
    inject({}, p, _b(location="query", name="api_key", format="{secret}"), "REAL")
    assert ("keep", "me") in p  # caller's other params preserved
    assert ("api_key", "REAL") in p and ("api_key", "caller") not in p  # injected wins


def test_cli_auth_places_string_like_env():
    h: dict[str, str] = {}
    inject(h, {}, _b(injector="cli_auth"), "TOK")
    assert h["Authorization"] == "Bearer TOK"


def test_secret_file_extracts_default_field():
    h: dict[str, str] = {}
    inject(h, {}, _b(injector="secret_file"), '{"access_token": "AT123", "refresh_token": "RT"}')
    assert h["Authorization"] == "Bearer AT123"


def test_secret_file_custom_field():
    h: dict[str, str] = {}
    inject(h, {}, _b(injector="secret_file", secret_field="token"), '{"token": "XYZ"}')
    assert h["Authorization"] == "Bearer XYZ"


def test_oauth_injects_access_token():
    h: dict[str, str] = {}
    inject(h, {}, _b(injector="oauth"), '{"access_token": "OAT", "expires_at": 123}')
    assert h["Authorization"] == "Bearer OAT"


def test_plain_developer_token_header():
    # google-ads shape: a second binding placing a non-bearer header.
    h: dict[str, str] = {}
    inject(h, {}, _b(name="developer-token", format="{secret}"), "DEV123")
    assert h["developer-token"] == "DEV123"


def test_invalid_json_raises():
    with pytest.raises(ValueError, match="not valid JSON"):
        inject({}, {}, _b(injector="secret_file"), "not json")


def test_missing_field_raises():
    with pytest.raises(ValueError, match="not found"):
        inject({}, {}, _b(injector="secret_file", secret_field="nope"), '{"access_token": "x"}')


def test_unknown_injector_raises():
    with pytest.raises(ValueError, match="unknown injector"):
        inject({}, {}, _b(injector="ghost"), "x")
