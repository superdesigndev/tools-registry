"""Curated OAuth provider registry — the providers treg itself holds an approved app for.

The generic connect flow (`POST /oauth/start`) takes a caller-supplied client_id/secret/URIs —
BYO mode, for any OAuth2 provider. This registry is the OTHER half: providers where **treg** owns
the registered app, so a user picks a provider and consents, supplying nothing.

That asymmetry is the point of a hosted registry. The gating cost on these platforms is not the
OAuth dance, it's the approval behind it — a Google Ads developer token, Meta App Review, the
LinkedIn Marketing Developer Platform. A user cannot self-serve those at any effort level; we
have already cleared them. BYO stays available for anyone who holds better access than we do.

**Scopes are per CAPABILITY, never per provider.** Someone connecting Search Console must never be
shown "See, edit, create, and delete your Google Ads accounts and data" — that consent screen loses
the user, and it asks for authority the capability doesn't need.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import get_settings


@dataclass(frozen=True)
class OAuthProvider:
    """One provider treg holds an app for.

    `client_id_setting` / `client_secret_setting` name attributes on Settings rather than raw env
    vars, so the credentials load from `.env` the same way every other treg setting does.
    """

    service: str  # stable id used in URLs and by the CLI
    display_name: str
    auth_uri: str
    token_uri: str
    scopes: dict[str, list[str]]  # capability -> the scopes that capability actually needs
    client_id_setting: str
    client_secret_setting: str
    base_url: str = ""  # upstream API root, so a successful connect can auto-provision the tool
    docs_url: str = ""

    # Resource discovery: after consent, which sites/properties/accounts can this credential act on?
    # `discover_path` is relative to base_url; `discover_key` is the JSON list field in the response;
    # `discover_id_field`/`discover_label_field` name the id and human label inside each row.
    discover_path: str = ""
    discover_key: str = ""
    discover_id_field: str = "id"
    discover_label_field: str = ""

    @property
    def capabilities(self) -> list[str]:
        return sorted(self.scopes)

    @property
    def supports_discovery(self) -> bool:
        return bool(self.discover_path and self.base_url)

    def scopes_for(self, capability: str) -> list[str]:
        try:
            return self.scopes[capability]
        except KeyError:
            raise ValueError(
                f"{self.service} has no capability {capability!r} "
                f"(known: {', '.join(self.capabilities)})"
            ) from None


# ---- the registry ------------------------------------------------------------------------
# One Google OAuth client covers Search Console, Analytics, Ads and Business Profile — but each is
# registered separately so a connect only ever requests its own capability's scopes.

GOOGLE_SEARCH_CONSOLE = OAuthProvider(
    service="google-search-console",
    display_name="Google Search Console",
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    scopes={
        # webmasters.readonly is NON-SENSITIVE: no Google verification, no OAuth user cap, and no
        # "unverified app" screen. Keep read the default so the common path stays gate-free.
        "read": ["https://www.googleapis.com/auth/webmasters.readonly"],
        "write": ["https://www.googleapis.com/auth/webmasters"],
    },
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://searchconsole.googleapis.com",
    docs_url="https://developers.google.com/webmaster-tools/v1/api_reference_index",
    # GSC returns {"siteEntry": [{"siteUrl": "...", "permissionLevel": "..."}]}
    discover_path="/webmasters/v3/sites",
    discover_key="siteEntry",
    discover_id_field="siteUrl",
    discover_label_field="siteUrl",
)

REGISTRY: dict[str, OAuthProvider] = {p.service: p for p in (GOOGLE_SEARCH_CONSOLE,)}

DEFAULT_CAPABILITY = "read"


def get(service: str) -> OAuthProvider | None:
    return REGISTRY.get(service)


def credentials(provider: OAuthProvider) -> tuple[str, str]:
    """treg's own client id/secret for this provider. Raises if the deployment hasn't set them —
    a provider without credentials is listed as unconfigured rather than failing mid-consent."""
    s = get_settings()
    client_id = getattr(s, provider.client_id_setting, "") or ""
    client_secret = getattr(s, provider.client_secret_setting, "") or ""
    if not (client_id and client_secret):
        raise ValueError(
            f"{provider.service} is not configured on this server "
            f"(set TREG_{provider.client_id_setting.upper()} and "
            f"TREG_{provider.client_secret_setting.upper()})"
        )
    return client_id, client_secret


def is_configured(provider: OAuthProvider) -> bool:
    try:
        credentials(provider)
    except ValueError:
        return False
    return True


def listing() -> list[dict]:
    """Every known provider, flagged with whether this deployment can actually run its flow."""
    return [
        {
            "service": p.service,
            "display_name": p.display_name,
            "capabilities": p.capabilities,
            "base_url": p.base_url,
            "docs_url": p.docs_url,
            "configured": is_configured(p),
        }
        for p in sorted(REGISTRY.values(), key=lambda p: p.display_name)
    ]
