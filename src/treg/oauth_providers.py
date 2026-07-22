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
    # A cheap authenticated GET on base_url that proves the credential still works, mirroring the
    # env-import catalog's `probe`. Registry tools had none, so they showed "unchecked" on the Tools
    # page forever — health could never say more than "nothing has called this yet". It must live on
    # base_url, NOT discover_base_url: the probe runs against the provisioned tool's own host.
    probe_path: str = ""

    # Per-provider auth quirks. Defaults match Google, which is the common case.
    auth_params: dict[str, str] | None = None  # extra ?query on the consent URL
    pkce: bool = False  # S256 challenge/verifier (X requires it)
    token_endpoint_auth_method: str = "client_secret_post"  # or client_secret_basic (X)

    # Some providers need a SECOND credential alongside the user's OAuth token — Google Ads wants a
    # developer-token header from an approved MCC. We can't auto-provision a working tool from the
    # OAuth alone, so we say what's missing and let the user supply it; once they do, the tool is
    # built with BOTH bindings and the connection becomes callable.
    extra_credential_note: str = ""
    extra_credential_label: str = ""  # what to call it in the UI, e.g. "Developer token"
    extra_credential_header: str = ""  # the header it's injected as, e.g. "developer-token"
    # Settings attribute holding TREG's own value for it. When set, users supply nothing and the
    # tool is provisioned with a platform binding; the per-user prompt is only the fallback.
    extra_credential_setting: str = ""

    @property
    def needs_extra_credential(self) -> bool:
        return bool(self.extra_credential_header)

    @property
    def platform_extra_credential(self) -> str:
        """treg's own value for the second credential, if this deployment has one."""
        if not self.extra_credential_setting:
            return ""
        return getattr(get_settings(), self.extra_credential_setting, "") or ""

    @property
    def extra_credential_is_platform(self) -> bool:
        return bool(self.platform_extra_credential)

    # Resource discovery: after consent, which sites/properties/accounts can this credential act on?
    # `resource_label` is what the thing is CALLED to a human — "site", "property", "account".
    # Never show the user the word "resource"; it means nothing outside this file.
    resource_label: str = "resource"
    resource_label_plural: str = ""  # defaults to label + "s"; set it when that's wrong ("properties")
    # Listing often lives on a different host than the data API (GA4 reports come from
    # analyticsdata, but its properties are listed by analyticsadmin), so discovery can override
    # the base URL. `discover_nested_key` expands a list nested inside each row.
    discover_base_url: str = ""  # defaults to base_url
    discover_path: str = ""
    discover_key: str = ""
    discover_nested_key: str = ""
    discover_id_field: str = "id"
    discover_label_field: str = ""

    # Some listings return only ids — Google Ads' listAccessibleCustomers gives
    # ["customers/6186675831", …] and nothing else. "6186675831" tells a user nothing about which
    # account they're choosing, so a provider can declare a per-row lookup for the human name.
    # `{id}` is the bare id (the last path segment of the resource id).
    enrich_path: str = ""  # POSTed to discovery_base + this
    enrich_body: dict | None = None
    enrich_label_path: str = ""  # dotted path into the response, e.g. "results.0.customer.name"
    enrich_header_name: str = ""  # optional per-row header, e.g. login-customer-id
    enrich_header_value: str = "{id}"

    @property
    def supports_enrichment(self) -> bool:
        return bool(self.enrich_path and self.enrich_label_path)

    # Some providers have nothing to CHOOSE between — a LinkedIn connection always acts as the one
    # member who consented. But which member that is still matters, so a provider can declare a
    # one-shot identity lookup run at connect time. It also captures the id the API needs (the
    # member URN), sparing the agent a round-trip it would otherwise make on every post.
    identity_path: str = ""
    identity_id_path: str = ""  # dotted path to the id, e.g. "sub"
    identity_label_path: str = ""  # dotted path to the display name, e.g. "name"
    identity_ref_format: str = "{id}"  # e.g. "urn:li:person:{id}"

    @property
    def has_identity(self) -> bool:
        return bool(self.identity_path and self.identity_id_path)

    @property
    def capabilities(self) -> list[str]:
        return sorted(self.scopes)

    @property
    def default_capability(self) -> str:
        """The capability a plain Connect asks for: the BROADEST one.

        Least-privilege-by-default sounds right but played badly. An agent product is asked to DO
        things, so most users need write eventually, and making them connect twice — once for read,
        once to widen it — is a worse experience than one honest consent screen. Users who want
        read-only can still pick it at connect time; capabilities are cumulative, so the broadest
        one contains the narrower ones."""
        return max(self.capabilities, key=lambda c: len(self.scopes[c]))

    @property
    def resource_plural(self) -> str:
        return self.resource_label_plural or f"{self.resource_label}s"

    @property
    def discovery_base(self) -> str:
        return self.discover_base_url or self.base_url

    @property
    def supports_discovery(self) -> bool:
        return bool(self.discover_path and self.discovery_base)

    @property
    def can_autoprovision(self) -> bool:
        """A tool we can build that will actually work with just this credential."""
        return bool(self.base_url) and (
            not self.needs_extra_credential or self.extra_credential_is_platform
        )

    def satisfied_capabilities(self, granted: list[str]) -> list[str]:
        """Which capabilities an existing grant already covers.

        Providers do not backfill scopes onto an issued grant, so adding a capability later means
        re-consenting. Comparing what was granted against what each capability needs is how we know
        to prompt for that instead of letting the call fail with an opaque 403."""
        have = set(granted)
        return [cap for cap, needed in sorted(self.scopes.items()) if set(needed) <= have]

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
        # write INCLUDES read: a capability is a superset, never a swap. Requesting only the
        # broader scope would leave a connection that can write but reports "no read".
        "write": [
            "https://www.googleapis.com/auth/webmasters.readonly",
            "https://www.googleapis.com/auth/webmasters",
        ],
    },
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://searchconsole.googleapis.com",
    docs_url="https://developers.google.com/webmaster-tools/v1/api_reference_index",
    # GSC returns {"siteEntry": [{"siteUrl": "...", "permissionLevel": "..."}]}
    resource_label="site",
    probe_path="/webmasters/v3/sites",
    discover_path="/webmasters/v3/sites",
    discover_key="siteEntry",
    discover_id_field="siteUrl",
    discover_label_field="siteUrl",
)

GOOGLE_ANALYTICS = OAuthProvider(
    service="google-analytics",
    display_name="Google Analytics",
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    scopes={"read": ["https://www.googleapis.com/auth/analytics.readonly"]},
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://analyticsdata.googleapis.com",
    docs_url="https://developers.google.com/analytics/devguides/reporting/data/v1",
    # No probe_path: the Data API is POST-only (runReport), and a probe must be a cheap GET on
    # base_url. Don't "fix" this by pointing at analyticsadmin — the probe runs against the
    # provisioned tool's own host, so it would test a host the tool never calls.
    # GA4 reports come from analyticsdata, but the property LIST lives on analyticsadmin, and the
    # properties are nested one level down inside each account summary.
    resource_label="property",
    resource_label_plural="properties",
    discover_base_url="https://analyticsadmin.googleapis.com",
    discover_path="/v1beta/accountSummaries",
    discover_key="accountSummaries",
    discover_nested_key="propertySummaries",
    discover_id_field="property",
    discover_label_field="displayName",
)

GOOGLE_BUSINESS_PROFILE = OAuthProvider(
    service="google-business-profile",
    display_name="Google Business Profile",
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    # business.manage is NON-SENSITIVE per Google's own console — no scope review. The gate here is
    # the separate Business Profile API access request, which starts every project at zero quota.
    scopes={"manage": ["https://www.googleapis.com/auth/business.manage"]},
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://mybusinessaccountmanagement.googleapis.com",
    docs_url="https://developers.google.com/my-business",
    resource_label="account",
    # base_url is mybusinessaccountmanagement, so the probe and the listing share a path here.
    probe_path="/v1/accounts",
    discover_path="/v1/accounts",
    discover_key="accounts",
    discover_id_field="name",
    discover_label_field="accountName",
)

GOOGLE_ADS = OAuthProvider(
    service="google-ads",
    display_name="Google Ads",
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    scopes={"manage": ["https://www.googleapis.com/auth/adwords"]},
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://googleads.googleapis.com",
    docs_url="https://developers.google.com/google-ads/api/docs/start",
    # Every Ads request carries TWO credentials: the user's OAuth bearer AND a `developer-token`
    # header from an approved manager (MCC) account, usually with `login-customer-id` as well.
    # Auto-provisioning a bearer-only tool would produce something that 401s on first use, so we
    # connect the credential and let the operator bind the developer token deliberately.
    extra_credential_note=(
        "Google Ads needs a developer token from your Google Ads manager (MCC) account as well as "
        "this sign-in. Add the token under Secrets, then bind it to the google-ads tool as a "
        "developer-token header."
    ),
    extra_credential_label="Developer token",
    extra_credential_header="developer-token",
    extra_credential_setting="google_ads_developer_token",
    # Which ad account should this connection act on? listAccessibleCustomers returns the accounts
    # the CONNECTED USER can reach — never ours.
    resource_label="account",
    probe_path="/v21/customers:listAccessibleCustomers",
    discover_path="/v21/customers:listAccessibleCustomers",
    discover_key="resourceNames",
    enrich_path="/v21/customers/{id}/googleAds:search",
    enrich_body={"query": "SELECT customer.descriptive_name FROM customer LIMIT 1"},
    enrich_label_path="results.0.customer.descriptiveName",
    enrich_header_name="login-customer-id",
)

# Every YouTube scope is SENSITIVE — there is no gate-free read the way webmasters.readonly is for
# Search Console, so this provider only works once the Google app clears verification. Uploads have
# a second, separate gate: until the project passes YouTube's compliance audit, videos.insert
# succeeds but the video is locked to private no matter what privacyStatus we send.
_YOUTUBE_READ = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

YOUTUBE = OAuthProvider(
    service="youtube",
    display_name="YouTube",
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",
    # Three capabilities because the gap between them is the whole story on YouTube: uploading a
    # video and being able to EDIT or DELETE one are different scopes. youtube.upload alone gets a
    # connection that can post and then never touch the post again, which is why `manage` exists.
    scopes={
        "read": _YOUTUBE_READ,
        "post": [*_YOUTUBE_READ, "https://www.googleapis.com/auth/youtube.upload"],
        "manage": [
            *_YOUTUBE_READ,
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
    },
    client_id_setting="google_client_id",
    client_secret_setting="google_client_secret",
    base_url="https://youtube.googleapis.com",
    docs_url="https://developers.google.com/youtube/v3/docs",
    # channels.list is 1 quota unit whatever `part` asks for, so take snippet: the Tools panel
    # prefills from this path, and a channel title reads better than an opaque UC… id. It 401s on a
    # dead token rather than returning an empty-but-successful list the way a bad filter would.
    probe_path="/youtube/v3/channels?part=snippet&mine=true",
    # Which channel does this connection post to? channels.list?mine=true answers for the connected
    # account. The title lives one level down in snippet, so the label is a dotted path.
    resource_label="channel",
    discover_path="/youtube/v3/channels?part=snippet&mine=true",
    discover_key="items",
    discover_id_field="id",
    discover_label_field="snippet.title",
)

LINKEDIN = OAuthProvider(
    service="linkedin",
    display_name="LinkedIn",
    auth_uri="https://www.linkedin.com/oauth/v2/authorization",
    token_uri="https://www.linkedin.com/oauth/v2/accessToken",
    # One capability: these scopes let the member read their own profile and post as themselves.
    # A read-only LinkedIn connection could do nothing but identify you, so offering the choice
    # would be a dialog with no real second option. Organization/page scopes need the Community
    # Management API on a company-verified app — a separate capability once that app is in use.
    scopes={"write": ["openid", "profile", "email", "w_member_social"]},
    client_id_setting="linkedin_client_id",
    client_secret_setting="linkedin_client_secret",
    base_url="https://api.linkedin.com",
    docs_url="https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin",
    auth_params={},  # LinkedIn rejects Google's access_type/prompt
    resource_label="member",
    # userinfo is the one LinkedIn path that needs no LinkedIn-Version header, so it survives their
    # quarterly version deprecations — a probe that rots is worse than no probe.
    probe_path="/v2/userinfo",
    identity_path="/v2/userinfo",
    identity_id_path="sub",
    identity_label_path="name",
    identity_ref_format="urn:li:person:{id}",
)

SLACK = OAuthProvider(
    service="slack",
    display_name="Slack",
    auth_uri="https://slack.com/oauth/v2/authorize",
    token_uri="https://slack.com/api/oauth.v2.access",
    scopes={
        "read": ["channels:read", "channels:history", "users:read"],
        # cumulative: write is read + posting, so choosing it never costs you read access
        "write": ["channels:read", "channels:history", "users:read", "chat:write"],
    },
    client_id_setting="slack_client_id",
    client_secret_setting="slack_client_secret",
    base_url="https://slack.com/api",
    docs_url="https://api.slack.com/web",
    probe_path="/auth.test",  # Slack's canonical "is this token good" call
    auth_params={},  # Slack rejects Google's access_type/prompt params
)

X = OAuthProvider(
    service="x",
    display_name="X (Twitter)",
    auth_uri="https://x.com/i/oauth2/authorize",
    token_uri="https://api.x.com/2/oauth2/token",
    # offline.access is what makes the credential auto-refreshable; without it every connection
    # becomes a manual-reconnect chore in ~2 hours.
    scopes={
        "read": ["tweet.read", "users.read", "offline.access"],
        "write": ["tweet.read", "tweet.write", "users.read", "offline.access"],
    },
    client_id_setting="x_client_id",
    client_secret_setting="x_client_secret",
    base_url="https://api.x.com",
    docs_url="https://docs.x.com/x-api",
    pkce=True,  # X rejects an authorization code exchanged without a verifier
    token_endpoint_auth_method="client_secret_basic",  # and rejects the secret in the body
    auth_params={},
    resource_label="account",
    # Same path as the identity lookup and the Try-panel sample: cheap, authenticated, and it
    # returns the handle rather than an opaque id.
    probe_path="/2/users/me",
    identity_path="/2/users/me",
    identity_id_path="data.id",
    identity_label_path="data.username",
)

REGISTRY: dict[str, OAuthProvider] = {
    p.service: p
    for p in (
        GOOGLE_SEARCH_CONSOLE, GOOGLE_ANALYTICS, GOOGLE_BUSINESS_PROFILE, GOOGLE_ADS, YOUTUBE,
        LINKEDIN, SLACK, X,
    )
}

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
            "default_capability": p.default_capability,
            "resource_label": p.resource_label,
            "resource_plural": p.resource_plural,
            "supports_discovery": p.supports_discovery,
            "has_identity": p.has_identity,
            "extra_credential_note": p.extra_credential_note,
            "extra_credential_label": p.extra_credential_label,
            "needs_extra_credential": p.needs_extra_credential,
            "base_url": p.base_url,
            "docs_url": p.docs_url,
            "configured": is_configured(p),
        }
        for p in sorted(REGISTRY.values(), key=lambda p: p.display_name)
    ]
