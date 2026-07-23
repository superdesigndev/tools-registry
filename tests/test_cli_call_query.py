"""cmd_call must not lose an inline `?query` written into the path.

httpx drops a URL's existing query string whenever params= is passed (even []), so before the fix
`treg call meta-ads "act_1?fields=name"` reached the proxy with NO query — the upstream returned
default/wrong data with no error. These tests capture the request cmd_call actually builds.
"""
from types import SimpleNamespace

import httpx

from treg import cli


def _capture(monkeypatch):
    """Swap cmd_call's client + output sink for captures; return the list the URL lands in."""
    seen: list[httpx.Request] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            # Build the same way httpx.Client would, so the query-merge behaviour is exercised.
            req = httpx.Request(method, httpx.URL("http://t").join(url), params=params)
            seen.append(req)
            return httpx.Response(200, json={})

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_show", lambda resp: None)
    return seen


def _args(target, path="", query=None):
    return SimpleNamespace(
        target=target, path=path, method="GET", query=query or [],
        data=None, file=None, content_type=None, header=[],
    )


def test_inline_query_is_not_dropped(monkeypatch):
    seen = _capture(monkeypatch)
    cli.cmd_call(_args("meta-ads", "act_1?fields=name,currency"), {})
    assert seen[0].url.params.get("fields") == "name,currency"


def test_inline_query_composes_with_query_flag(monkeypatch):
    seen = _capture(monkeypatch)
    cli.cmd_call(_args("meta-ads", "act_1/campaigns?fields=name,status", query=["limit=2"]), {})
    p = seen[0].url.params
    assert p.get("fields") == "name,status" and p.get("limit") == "2"


def test_no_query_still_clean(monkeypatch):
    seen = _capture(monkeypatch)
    cli.cmd_call(_args("meta-ads", "act_1/campaigns"), {})
    assert not seen[0].url.params and seen[0].url.path == "/call/meta-ads/act_1/campaigns"
