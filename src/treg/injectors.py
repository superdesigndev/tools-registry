"""The injector contract — the seam that keeps the proxy core dumb.

A tool carries a LIST of bindings; the proxy applies each. A binding is a plain dict:
    {secret_id, injector, location: "header"|"query", name, format, secret_field}
The proxy never branches on auth shape — it calls INJECTORS[binding["injector"]] per binding.
Underneath there are two mechanics: place a string (env, cli_auth) or pull a field from a
JSON blob (secret_file, oauth). Acquisition (CLI keychain / OAuth handshake / token file) is
onboarding's job. Adding a shape never touches the proxy.
"""

from __future__ import annotations

import json
from collections.abc import Callable

# An injector places one decrypted secret into the outgoing (headers, params) per its binding.
Injector = Callable[[dict[str, str], dict[str, str], dict, str], None]

INJECTORS: dict[str, Injector] = {}


def register(name: str) -> Callable[[Injector], Injector]:
    def deco(fn: Injector) -> Injector:
        INJECTORS[name] = fn
        return fn

    return deco


def _place(headers, params: list, binding: dict, value: str) -> None:
    """Put `value` where the binding declares. `headers` is a mapping that overwrites by name
    (dict or httpx.Headers); `params` is a list of (k, v) pairs (preserves duplicate caller
    params). For a query binding we drop any caller param of the same name so the injected
    credential wins, then append it."""
    rendered = binding.get("format", "{secret}").format(secret=value)
    name = binding.get("name", "Authorization")
    if binding.get("location", "header") == "query":
        params[:] = [(k, v) for (k, v) in params if k != name]
        params.append((name, rendered))
    else:
        headers[name] = rendered


def _token_from_json(blob: str, field: str) -> str:
    """Pull a token field out of a stored JSON secret (a `.secret`/OAuth token file)."""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError("secret is not valid JSON for this injector") from exc
    if not isinstance(data, dict) or field not in data:
        raise ValueError(f"field {field!r} not found in secret JSON")
    val = data[field]
    if not isinstance(val, (str, int, float)) or isinstance(val, bool):
        # str(dict/list/None/bool) would inject garbage ("{'x': 1}", "None", "True") as the
        # credential — a confusing upstream 401 instead of a clear config error.
        raise ValueError(f"field {field!r} is {type(val).__name__}, expected a string token")
    return str(val)


# ---- string-value shapes ------------------------------------------------------------------
@register("env")
def env_injector(headers: dict[str, str], params: list, binding: dict, secret: str) -> None:
    """Plain-string credential (ENV-style)."""
    _place(headers, params, binding, secret)


@register("cli_auth")
def cli_auth_injector(headers: dict[str, str], params: list, binding: dict, secret: str) -> None:
    """Material lifted from a CLI's own config/keychain (e.g. stripe/gh). Placed like a string;
    the CLI-specific *extraction* happens during onboarding, not here."""
    _place(headers, params, binding, secret)


# ---- JSON-blob shapes ---------------------------------------------------------------------
@register("secret_file")
def secret_file_injector(headers: dict[str, str], params: list, binding: dict, secret: str) -> None:
    """A `.secret/` token file (GCP, Google Ads, GSC): pull the field and place it."""
    _place(headers, params, binding, _token_from_json(secret, binding.get("secret_field", "access_token")))


@register("oauth")
def oauth_injector(headers: dict[str, str], params: list, binding: dict, secret: str) -> None:
    """A stored OAuth token JSON: inject the access token.

    Auto-refresh on expiry is intentionally NOT here — refreshing is network + persistence,
    which belongs to the OAuth connect flow (Step 5), not to the hot injection path.
    """
    _place(headers, params, binding, _token_from_json(secret, binding.get("secret_field", "access_token")))


def inject(headers: dict[str, str], params: list, binding: dict, secret: str) -> None:
    injector = INJECTORS.get(binding.get("injector", "env"))
    if injector is None:
        raise ValueError(f"unknown injector: {binding.get('injector')!r}")
    injector(headers, params, binding, secret)
