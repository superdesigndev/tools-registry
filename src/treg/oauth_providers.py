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
    # Which shelf this sits on in the marketplace. A flat list of eleven providers reads as a pile;
    # grouped, someone connecting a social account never scans past four analytics tools. It lives
    # here rather than in the dashboard so a new provider can't be left silently ungrouped — the
    # default lands it in "Other", which is visible enough to get noticed and fixed.
    # CATEGORY_ORDER below decides the order the shelves appear in.
    category: str = "Other"
    # One line for the marketplace card: what an agent can actually DO with this connected, in
    # plain terms. Not a tagline — someone scanning a grid of twenty is deciding whether this is
    # the thing that answers their question.
    summary: str = ""
    # ---- how the credential is obtained --------------------------------------------------
    # "oauth"  — treg holds an approved app; the user consents and supplies nothing.
    # "token"  — the user brings their OWN bot/app token. Correct where a workspace-scoped bot
    #            is the natural unit (Slack): our app can't be installed into their workspace on
    #            their behalf, and a shared app would put treg between them and their own data.
    #            Setup is a form, not a redirect, so the provider carries the instructions.
    auth_kind: str = "oauth"
    token_label: str = ""  # "Bot token"
    token_placeholder: str = ""  # "xoxb-…"
    token_header: str = "Authorization"
    token_format: str = "Bearer {secret}"
    # Where the pasted credential rides. "header" (default) injects it as token_header; "query"
    # injects it as the token_param query parameter — Semrush authenticates the classic API with
    # `?key=…`, not a header. Drives both the connect-time probe and the provisioned tool's binding.
    token_location: str = "header"  # "header" | "query"
    token_param: str = ""  # query-param name when token_location == "query" (Semrush: "key")
    setup_url: str = ""  # one-click app creation, pre-filled where the platform supports it
    setup_action_label: str = ""
    setup_steps: tuple[str, ...] = ()
    setup_note: str = ""
    # Where a token provider reports the scopes it was actually granted. There is no consent
    # response to read them from, so without this a connection claims "0 scopes" while holding a
    # perfectly well-scoped token.
    token_scopes_header: str = ""
    base_url: str = ""  # upstream API root, so a successful connect can auto-provision the tool
    # Copy-paste sample calls stamped onto the provisioned tool's `examples`, surfaced by
    # `tool ls`. The single most useful thing to carry here is the API VERSION: Google's REST APIs
    # version the URL path (v21/...) and a wrong guess returns an HTML 404, not a hint — agents
    # otherwise burn calls guessing. `{resource}` is a placeholder the agent substitutes.
    examples: tuple[dict, ...] = ()
    docs_url: str = ""
    # A cheap authenticated GET on base_url that proves the credential still works, mirroring the
    # env-import catalog's `probe`. Registry tools had none, so they showed "unchecked" on the Tools
    # page forever — health could never say more than "nothing has called this yet". It must live on
    # base_url, NOT discover_base_url: the probe runs against the provisioned tool's own host.
    probe_path: str = ""
    # An ABSOLUTE URL to verify a pasted key against, used only at connect time when the cheapest
    # key-check lives on a DIFFERENT host than base_url — Semrush's free unit-balance endpoint is on
    # www.semrush.com, not the api.semrush.com data host. When empty the connect probe is
    # base_url + probe_path. This does not become the tool's ongoing health probe (that is probe_path).
    probe_url: str = ""

    # Per-provider auth quirks. Defaults match Google, which is the common case.
    auth_params: dict[str, str] | None = None  # extra ?query on the consent URL
    pkce: bool = False  # S256 challenge/verifier (X requires it)
    token_endpoint_auth_method: str = "client_secret_post"  # or client_secret_basic (X)
    # OAuth2 says the client identifier is `client_id` and scopes are space-delimited. TikTok obeys
    # neither: it reads `client_key` and splits scopes on commas. Both are snapshotted onto the
    # PendingOAuth so the callback and every later refresh speak the same dialect as the consent URL.
    client_id_param: str = "client_id"  # TikTok: "client_key"
    scope_separator: str = " "  # TikTok: ","
    # Meta hands back a ~1-2 HOUR user token from the authorization-code exchange and never issues
    # a refresh_token. Left alone, every Meta connection would be dead before the user finished
    # reading the success page. A second call — grant_type=fb_exchange_token — swaps it for a
    # ~60-day token, which is the longest Meta will give a user credential. That still can't be
    # renewed unattended, so the connection surfaces through the same `needs_reconnect` path as
    # LinkedIn's non-refreshable tokens rather than pretending it auto-heals.
    long_lived_exchange: bool = False

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
    def is_token_kind(self) -> bool:
        return self.auth_kind == "token"

    @property
    def uses_pasted_secret(self) -> bool:
        """A provider the user connects by PASTING a credential — a bring-your-own bot token
        (Slack, `auth_kind="token"`) or a plain API key (`auth_kind="key"`). Both share one connect
        path: verify the credential against a probe, store it as an env secret, auto-provision the
        tool. They differ only in the marketplace copy and, for a key, the header/query it rides in.
        `is_token_kind` stays narrower — it gates the Slack-only bot-setup wording."""
        return self.auth_kind in ("token", "key")

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
        # A token provider has no consent screen to size, so no capabilities — don't max() an
        # empty sequence and take the whole /oauth/providers listing down with it.
        if not self.scopes:
            return ""
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
    category="SEO",
    summary=(
        "Which queries and pages bring you organic traffic, what's indexed, and how rankings move over time."
    ),
    base_url="https://searchconsole.googleapis.com",
    docs_url="https://developers.google.com/webmaster-tools/v1/api_reference_index",
    examples=(
        {"method": "POST", "path": "webmasters/v3/sites/{site_url}/searchAnalytics/query",
         "note": "Search analytics. {site_url} is sc-domain:example.com or https://example.com/. "
                 "Body: {\"startDate\":\"2026-06-01\",\"endDate\":\"2026-06-28\","
                 "\"dimensions\":[\"query\"]}. For a site TOTAL, omit dimensions — summing a "
                 "dimension does NOT equal the total."},
        {"method": "POST", "path": "v1/urlInspection/index:inspect",
         "note": "Index status — note the v1/ prefix, not webmasters/v3/. "
                 "Body: {\"inspectionUrl\":\"https://example.com/page\",\"siteUrl\":\"sc-domain:example.com\"}"},
    ),
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
    category="SEO",
    summary=(
        "Sessions, users, conversions and traffic sources — run any GA4 report your agent can describe."
    ),
    base_url="https://analyticsdata.googleapis.com",
    docs_url="https://developers.google.com/analytics/devguides/reporting/data/v1",
    examples=(
        {"method": "POST", "path": "v1beta/properties/{property_id}:runReport",
         "note": "Data API v1beta. Body: {\"dateRanges\":[{\"startDate\":\"28daysAgo\","
                 "\"endDate\":\"yesterday\"}],\"dimensions\":[{\"name\":\"pagePath\"}],"
                 "\"metrics\":[{\"name\":\"screenPageViews\"}]}. Use 'yesterday', not 'today' "
                 "(today is a partial day). The Admin API (property listing) is a different host — "
                 "use `treg connections resources`."},
    ),
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
    category="SEO",
    summary=(
        "Your listings, reviews and local posts. Read what customers are saying and reply as the business."
    ),
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
    # Ads gets its OWN OAuth client, in a DIFFERENT Cloud project from the other Google providers.
    # A Google Ads developer token is permanently paired to the first Cloud project it calls from,
    # and the shared Google project is already welded to a different (stale) token — so Ads must
    # consent through a client in the same Cloud project the live developer token is paired with,
    # or the API rejects it with DEVELOPER_TOKEN_PROHIBITED. This is the only provider that doesn't
    # share google_client_id.
    client_id_setting="google_ads_client_id",
    client_secret_setting="google_ads_client_secret",
    category="Advertising",
    summary=(
        "Campaign spend, performance and keyword data across your accounts — and change campaigns when you're ready."
    ),
    base_url="https://googleads.googleapis.com",
    docs_url="https://developers.google.com/google-ads/api/docs/start",
    examples=(
        {"method": "POST", "path": "v21/customers/{customer_id}/googleAds:search",
         "note": "GAQL read. API version v21 (verified 2026-07-22); a wrong version 404s as HTML. "
                 "Body: {\"query\":\"SELECT campaign.name, metrics.cost_micros FROM campaign "
                 "WHERE segments.date DURING LAST_30_DAYS\"}"},
        {"method": "POST", "path": "v21/customers/{customer_id}/campaignBudgets:mutate",
         "note": "Mutate. Add \"validateOnly\":true first to dry-run. amountMicros: $1 = 1000000."},
    ),
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
    category="Social media",
    summary=(
        "Channel, video and playlist data with watch time and revenue reports. Upload and manage videos too."
    ),
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
    category="Social media",
    summary=(
        "Post to your feed as yourself and read back how it performed."
    ),
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
    # Bring-your-own-bot, not a treg-owned OAuth app. A Slack bot is workspace-scoped and belongs
    # to the workspace it's installed in — a shared treg app would sit between a team and their own
    # messages, and could never be installed on their behalf anyway. So the user creates a bot
    # (one click, pre-filled manifest) and pastes its token.
    auth_kind="token",
    token_label="Bot token",
    token_placeholder="xoxb-…",
    setup_url='https://api.slack.com/apps?new_app=1&manifest_json=%7B%22display_information%22%3A%20%7B%22name%22%3A%20%22treg%22%2C%20%22description%22%3A%20%22Let%20your%20AI%20agent%20read%20and%20post%20in%20Slack%2C%20with%20the%20token%20held%20server-side.%22%7D%2C%20%22features%22%3A%20%7B%22bot_user%22%3A%20%7B%22display_name%22%3A%20%22treg%22%7D%7D%2C%20%22oauth_config%22%3A%20%7B%22scopes%22%3A%20%7B%22bot%22%3A%20%5B%22chat%3Awrite%22%2C%20%22chat%3Awrite.public%22%2C%20%22channels%3Aread%22%2C%20%22groups%3Aread%22%2C%20%22im%3Aread%22%2C%20%22mpim%3Aread%22%2C%20%22channels%3Ahistory%22%2C%20%22groups%3Ahistory%22%2C%20%22users%3Aread%22%2C%20%22reactions%3Aread%22%2C%20%22reactions%3Awrite%22%2C%20%22files%3Aread%22%2C%20%22app_mentions%3Aread%22%5D%7D%7D%7D',
    setup_action_label="Create the Slack app (pre-filled)",
    setup_steps=(
        "Click the button above — it opens Slack with the bot and scopes already configured. "
        "Pick your workspace and hit Create.",
        "On the app page click \"Install to Workspace\" and allow it.",
        "Open OAuth & Permissions and copy the Bot User OAuth Token (xoxb-…) — "
        "NOT the App-Level Token (xapp-…).",
    ),
    setup_note="Public channels work immediately. For a private channel, /invite the bot first.",
    token_scopes_header="x-oauth-scopes",
    auth_uri="", token_uri="",
    scopes={},  # scopes live in the manifest above; there is no consent screen to size
    client_id_setting="", client_secret_setting="",
    category="Community",
    summary=(
        "Read and post messages in your workspace with a bot you create and control."
    ),
    base_url="https://slack.com/api",
    docs_url="https://api.slack.com/web",
    probe_path="/auth.test",
    # No channel picker. `chat.postMessage` takes the channel per call, and the agent can list
    # channels itself through the proxy — so choosing one here duplicated a capability it already
    # has in order to store a preference nothing enforces. Providers where the resource is in the
    # request URL (a Search Console site, a GA property) keep theirs: there the human is making a
    # real choice the agent would otherwise have to guess on every call.
    # auth.test names the workspace and the bot, so a connection still says which Slack it is.
    identity_path="/auth.test",
    identity_id_path="team_id",
    identity_label_path="team",
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
    category="Social media",
    summary=(
        "Read posts and timelines, and publish as your account."
    ),
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

# TikTok grants scopes through PRODUCTS, not à la carte: user.info.basic rides on Login Kit,
# video.upload on the Content Posting API, and video.publish only appears once that product's
# "Direct Post" toggle is on. So this scope set is really a statement about the portal config, and
# the two must be changed together — asking here for a scope the app doesn't carry fails at consent
# with scope_not_authorized rather than at build time.
# These four must stay in lockstep with the consent screen in the submitted demo video — TikTok
# rejects an app whose requested scopes exceed what the video shows, and each scope is a visible
# line on that screen. Adding one here means re-recording.
_TIKTOK_READ = ["user.info.basic", "user.info.profile", "video.list", "user.info.stats"]

TIKTOK = OAuthProvider(
    service="tiktok",
    display_name="TikTok",
    auth_uri="https://www.tiktok.com/v2/auth/authorize/",
    token_uri="https://open.tiktokapis.com/v2/oauth/token/",
    # draft and post are a real split, not a nicety: video.upload only puts the video in the
    # creator's inbox for them to finish by hand (and TikTok discards it after 24h), while
    # video.publish posts to the profile outright. A caller that wants review-before-publish
    # genuinely must not hold video.publish.
    scopes={
        "read": _TIKTOK_READ,
        "draft": [*_TIKTOK_READ, "video.upload"],
        "post": [*_TIKTOK_READ, "video.upload", "video.publish"],
    },
    client_id_setting="tiktok_client_id",
    client_secret_setting="tiktok_client_secret",
    category="Social media",
    summary=(
        "Your videos, follower and engagement stats, plus direct publishing."
    ),
    base_url="https://open.tiktokapis.com",
    docs_url="https://developers.tiktok.com/doc/login-kit-web/",
    client_id_param="client_key",  # not client_id — TikTok ignores the OAuth2 spelling
    scope_separator=",",  # not a space
    auth_params={},  # TikTok rejects Google's access_type/prompt
    resource_label="account",
    # One connection = one authorized creator, so there is nothing to pick; identity_* labels it
    # instead of showing an empty picker. Cheap, authenticated, and returns a human name.
    probe_path="/v2/user/info/?fields=open_id,display_name",
    identity_path="/v2/user/info/?fields=open_id,display_name",
    identity_id_path="data.user.open_id",
    identity_label_path="data.user.display_name",
)

# Both Meta providers speak to the same host with the same app; they differ only in which asset the
# connection acts on (a Page vs an Instagram professional account) and therefore which scopes it
# needs. Kept as two providers rather than one with capabilities, because a user connecting
# Instagram must never see "manage your Facebook Pages' posts" on the consent screen.
_META_AUTH = "https://www.facebook.com/v25.0/dialog/oauth"
_META_TOKEN = "https://graph.facebook.com/v25.0/oauth/access_token"
_META_BASE = "https://graph.facebook.com/v25.0"

# pages_show_list is the floor for BOTH providers: it is what returns the Page list, and an
# Instagram professional account is only reachable *through* the Page it is linked to.
_FB_READ = ["pages_show_list", "pages_read_engagement", "read_insights"]

FACEBOOK = OAuthProvider(
    service="facebook",
    display_name="Facebook Pages",
    auth_uri=_META_AUTH,
    token_uri=_META_TOKEN,
    # read covers listing Pages, reading their content and their insights — Meta has no separate
    # analytics-only tier worth splitting out, and a Pages connection that cannot read insights is
    # not a useful read. post adds the one scope that actually publishes.
    scopes={
        "read": _FB_READ,
        "post": [*_FB_READ, "pages_manage_posts"],
    },
    client_id_setting="meta_client_id",
    client_secret_setting="meta_client_secret",
    category="Social media",
    summary=(
        "Your Pages' posts, comments and reach — and publishing to them."
    ),
    base_url=_META_BASE,
    docs_url="https://developers.facebook.com/docs/pages-api",
    auth_params={},  # Meta ignores Google's access_type/prompt; sending them just noises the URL
    long_lived_exchange=True,
    resource_label="Page",
    # A user can administer several Pages, so which one this connection acts on is a real choice.
    discover_path="/me/accounts?fields=id,name",
    discover_key="data",
    discover_id_field="id",
    discover_label_field="name",
    # /me returns the person, not the Page, and needs no extra scope — so it keeps working even for
    # a connection whose Page was later unassigned, which is exactly when you want the probe to
    # still distinguish "credential dead" from "asset gone".
    probe_path="/me?fields=id,name",
)

INSTAGRAM = OAuthProvider(
    service="instagram",
    display_name="Instagram",
    auth_uri=_META_AUTH,
    token_uri=_META_TOKEN,
    # instagram_basic alone cannot publish, and instagram_content_publish alone cannot read the
    # account it publishes to — Meta enforces that dependency in App Review, so post is a strict
    # superset rather than a swap.
    scopes={
        "read": ["instagram_basic", "instagram_manage_insights", "pages_show_list", "pages_read_engagement"],
        "post": [
            "instagram_basic", "instagram_manage_insights", "pages_show_list",
            "pages_read_engagement", "instagram_content_publish",
        ],
    },
    client_id_setting="meta_client_id",
    client_secret_setting="meta_client_secret",
    category="Social media",
    summary=(
        "Your Instagram media, comments and insights, plus publishing to your account."
    ),
    base_url=_META_BASE,
    docs_url="https://developers.facebook.com/docs/instagram-platform/instagram-graph-api",
    auth_params={},
    long_lived_exchange=True,
    resource_label="account",
    resource_label_plural="accounts",
    # There is no endpoint that lists Instagram accounts directly: you list Pages and read the
    # professional account linked to each. Pages with no linked account come back with the field
    # absent, so the dotted id path yields nothing for them and they drop out of the picker.
    discover_path="/me/accounts?fields=instagram_business_account{id,username}",
    discover_key="data",
    discover_id_field="instagram_business_account.id",
    discover_label_field="instagram_business_account.username",
    probe_path="/me?fields=id,name",
)

META_ADS = OAuthProvider(
    service="meta-ads",
    display_name="Meta Ads",
    auth_uri=_META_AUTH,
    token_uri=_META_TOKEN,
    # business_management is in BOTH capabilities, not just manage: /me/adaccounts is a Business
    # asset listing, so without it a read-only connect consents fine and then has nothing to pick.
    # Unlike Google Ads this needs no second credential — Meta has no developer-token equivalent, so
    # a connect here yields a callable tool on its own.
    scopes={
        "read": ["ads_read", "business_management"],
        "manage": ["ads_read", "business_management", "ads_management"],
    },
    client_id_setting="meta_client_id",
    client_secret_setting="meta_client_secret",
    category="Advertising",
    summary=(
        "Ad accounts, campaigns and performance across Facebook and Instagram, with full campaign management."
    ),
    base_url=_META_BASE,
    docs_url="https://developers.facebook.com/docs/marketing-apis",
    auth_params={},
    long_lived_exchange=True,
    resource_label="ad account",
    resource_label_plural="ad accounts",
    # Returns act_<id> together with the account's name, so the picker shows "Superdesign Pty Ltd"
    # rather than an opaque number — no enrichment pass needed, unlike Google Ads.
    discover_path="/me/adaccounts?fields=id,name,account_id",
    discover_key="data",
    discover_id_field="id",
    discover_label_field="name",
    probe_path="/me?fields=id,name",
)

# ---- API-key providers (auth_kind="key") ------------------------------------------------------
# The user pastes an API key instead of consenting through an OAuth app treg owns. Same connect
# mechanic as Slack's bot token — verify against a probe, store as an env secret, auto-provision the
# tool — differing only in the header (or query param) the key rides in and the "where do I get a
# key" copy. A key provider needs nothing from treg, so it is always offerable (is_configured=True).
# No `scopes`: there is no consent screen to size, so the marketplace card leans on `summary`.

APOLLO = OAuthProvider(
    service="apollo",
    display_name="Apollo.io",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Apollo API key",
    token_header="X-Api-Key",
    token_format="{secret}",  # raw key, no Bearer prefix
    setup_url="https://developers.apollo.io/keys",
    setup_action_label="Get your Apollo API key",
    setup_steps=(
        "Sign in to Apollo and open Settings → Integrations → API.",
        "Create an API key (a master key reaches every endpoint) and copy it.",
    ),
    setup_note="Enrichment calls spend Apollo credits; the health check does not.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Enrich people and companies and search Apollo's 200M+ B2B contact database.",
    base_url="https://api.apollo.io/api/v1",
    docs_url="https://docs.apollo.io/reference/authentication",
    # Free auth check. Apollo documents the health probe at the legacy /v1/auth/health (no /api);
    # /api/v1/auth/health has historically resolved too. Confirm against a real key and adjust if it 404s.
    probe_path="/auth/health",
)

PDL = OAuthProvider(
    service="pdl",
    display_name="People Data Labs",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your People Data Labs API key",
    token_header="X-Api-Key",
    token_format="{secret}",
    setup_url="https://dashboard.peopledatalabs.com/main/api-keys",
    setup_action_label="Get your People Data Labs API key",
    setup_steps=(
        "Sign in to the People Data Labs dashboard and open API Keys.",
        "Copy your API key.",
    ),
    setup_note="Enrichment and search spend credits; the autocomplete health check is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Enrich a person or company, or search PDL's people and company datasets.",
    base_url="https://api.peopledatalabs.com/v5",
    docs_url="https://docs.peopledatalabs.com/docs/authentication",
    probe_path="/autocomplete?field=title&text=data",  # Autocomplete API is free (no credits)
)

AKTA = OAuthProvider(
    service="akta",
    display_name="Akta by Wokelo",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Akta API key",
    token_header="x-api-key",
    token_format="{secret}",
    setup_url="https://akta.pro",
    setup_action_label="Get your Akta API key",
    setup_steps=(
        "Request an API key for your Akta account (support@akta.pro).",
        "Paste it here.",
    ),
    setup_note="Company enrichment spends credits; company search is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Company intelligence — enrichment, industry resolution, reviews and news monitoring.",
    # base_url is api.akta.pro/api and every path carries its OWN /v1 prefix, so the effective path is
    # /api/v1/…. Setting base_url to /api/v1 would double the version to /api/v1/v1.
    base_url="https://api.akta.pro/api",
    docs_url="https://docs.akta.pro",
    probe_path="/v1/company/search?query=canva.com",  # documented free endpoint
)

HUNTER = OAuthProvider(
    service="hunter",
    display_name="Hunter",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Hunter API key",
    # Hunter accepts the key as ?api_key=…, an X-API-KEY header, or a Bearer header. Use the header
    # so the key never lands in a URL (the proxy records request paths; a query key could leak there).
    token_header="X-API-KEY",
    token_format="{secret}",
    setup_url="https://hunter.io/api-keys",
    setup_action_label="Get your Hunter API key",
    setup_steps=(
        "Sign in to Hunter and open API → API Keys.",
        "Copy your API key.",
    ),
    setup_note="Searches and verifications spend credits; the account check is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Find and verify professional email addresses, and enrich people and companies.",
    base_url="https://api.hunter.io/v2",
    docs_url="https://hunter.io/api-documentation/v2",
    probe_path="/account",  # free — consumes no search/verification/enrichment credits
)

TIKHUB = OAuthProvider(
    service="tikhub",
    display_name="TikHub",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your TikHub API key",
    # token_header / token_format default to Authorization: Bearer {secret}
    setup_url="https://tikhub.io/users/api_keys",
    setup_action_label="Get your TikHub API key",
    setup_steps=(
        "Sign in to TikHub and open the API Keys page.",
        "Create a key and copy it.",
    ),
    setup_note="Data calls are billed per successful request; the account check is not.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Social media",
    summary="Read TikTok, Instagram, YouTube, X and more social platforms through one unified API.",
    base_url="https://api.tikhub.io",
    docs_url="https://docs.tikhub.io/",
    probe_path="/api/v1/tikhub/user/get_user_info",  # account info — the natural key check
)

BRIGHTDATA = OAuthProvider(
    service="brightdata",
    display_name="Bright Data",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Bright Data API token",
    # Authorization: Bearer {secret} (defaults)
    setup_url="https://brightdata.com/cp/setting/users",
    setup_action_label="Get your Bright Data API token",
    setup_steps=(
        "Sign in to Bright Data and open Account settings → API tokens.",
        "Create a token and copy it.",
    ),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Social media",
    summary="Scrape social platforms and the web through Bright Data's Web Scraper API.",
    # Several product APIs share one host and one Bearer scheme; the social entry is pinned to the
    # Web Scraper API (/datasets/v3/…).
    base_url="https://api.brightdata.com",
    docs_url="https://docs.brightdata.com/api-reference/authentication",
    # Lightweight dataset listing — inferred (medium confidence). Verify with a real token; adjust if it 404s.
    probe_path="/datasets/v3/datasets",
)

SEMRUSH = OAuthProvider(
    service="semrush",
    display_name="Semrush",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Semrush API key",
    token_location="query",  # Semrush authenticates the classic API with ?key=…, not a header
    token_param="key",
    token_format="{secret}",
    setup_url="https://www.semrush.com/accounts/subscription-info/api-units/",
    setup_action_label="Get your Semrush API key",
    setup_steps=(
        "Sign in to Semrush and open Subscription info → API units.",
        "Copy your API key.",
    ),
    setup_note="Reports spend API units; the key check reads your unit balance for free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Domain, keyword and backlink analytics across Semrush's SEO database.",
    base_url="https://api.semrush.com/",
    docs_url="https://developer.semrush.com/api/v3/analytics/basic-docs/",
    # The free unit-balance check lives on a DIFFERENT host than the data API, so verify against it
    # directly. No probe_path: the classic API is CSV-only with no free GET on api.semrush.com, so the
    # provisioned tool carries no ongoing health probe (one would spend API units on every run).
    probe_url="https://www.semrush.com/users/countapiunits.html",
)

REGISTRY: dict[str, OAuthProvider] = {
    p.service: p
    for p in (
        GOOGLE_SEARCH_CONSOLE, GOOGLE_ANALYTICS, GOOGLE_BUSINESS_PROFILE, GOOGLE_ADS, YOUTUBE,
        LINKEDIN, SLACK, X, TIKTOK, FACEBOOK, INSTAGRAM, META_ADS,
        # API-key providers
        APOLLO, PDL, AKTA, HUNTER, TIKHUB, BRIGHTDATA, SEMRUSH,
    )
}

DEFAULT_CAPABILITY = "read"

# Shelf order in the marketplace. Anything carrying a category not named here sorts last, so a
# provider added without one is visible rather than lost between the shelves.
CATEGORY_ORDER = ("SEO", "Advertising", "Social media", "Enrichment", "Community", "Other")


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
    """Whether THIS deployment can offer the provider. A pasted-secret provider (bot token or API
    key) needs nothing from us — the user brings their own — so it is always offerable."""
    if provider.uses_pasted_secret:
        return True
    try:
        credentials(provider)
    except ValueError:
        return False
    return True


# Plain English for every scope we request. The raw string is what the provider returns and what
# the consent screen shows in fine print; this is what a human needs to decide whether to grant it.
# Keyed by the scope alone — safe today because no two providers request the same string with
# different meanings, and the OIDC ones (openid/profile/email) mean the same thing everywhere.
# `test_every_requested_scope_has_a_label` fails if a provider adds a scope and forgets the copy.
SCOPE_LABELS: dict[str, str] = {
    # Google — Search Console
    "https://www.googleapis.com/auth/webmasters.readonly":
        "See your verified sites, search performance, indexing status and sitemaps",
    "https://www.googleapis.com/auth/webmasters":
        "Submit and delete sitemaps, and manage your sites",
    # Google — Analytics / Ads / Business Profile
    "https://www.googleapis.com/auth/analytics.readonly":
        "Read your Analytics properties and run reports",
    "https://www.googleapis.com/auth/business.manage":
        "Manage your business listings, reviews and posts",
    "https://www.googleapis.com/auth/adwords":
        "Read campaigns, spend and performance, and manage campaigns",
    # Google — YouTube
    "https://www.googleapis.com/auth/youtube.readonly":
        "See your channel, videos and playlists",
    "https://www.googleapis.com/auth/yt-analytics.readonly":
        "Read your channel's views, watch time and revenue reports",
    "https://www.googleapis.com/auth/youtube.upload": "Upload videos to your channel",
    "https://www.googleapis.com/auth/youtube": "Manage your channel, videos and playlists",
    "https://www.googleapis.com/auth/youtube.force-ssl":
        "Manage your videos, comments and captions",
    # LinkedIn
    "openid": "Confirm who you are",
    "profile": "See your name and profile picture",
    "email": "See your email address",
    "w_member_social": "Post, comment and react as you",
    # X
    "tweet.read": "Read posts and timelines",
    "users.read": "See profiles, including your own",
    "offline.access": "Stay connected without asking you to sign in again",
    "tweet.write": "Post, reply and delete as you",
    # TikTok
    "user.info.basic": "See your account's basic profile",
    "user.info.profile": "See your display name, bio and avatar",
    "user.info.stats": "See your follower, like and video counts",
    "video.list": "List your published videos",
    "video.upload": "Upload videos to your account as drafts",
    "video.publish": "Publish videos directly to your account",
    # Meta — Facebook Pages
    "pages_show_list": "See which Pages you manage",
    "pages_read_engagement": "Read your Pages' posts, comments and reactions",
    "read_insights": "Read your Pages' reach and engagement insights",
    "pages_manage_posts": "Create, edit and delete posts on your Pages",
    # Meta — Instagram
    "instagram_basic": "See your Instagram account, media and comments",
    "instagram_manage_insights": "Read your Instagram reach and engagement insights",
    "instagram_content_publish": "Publish posts to your Instagram account",
    # Meta — Ads
    "ads_read": "Read your ad accounts, campaigns and performance",
    "business_management": "See the businesses and ad accounts you have access to",
    "ads_management": "Create and change campaigns, ad sets and ads",
}


def scope_label(scope: str) -> str:
    """Plain English for a scope, falling back to the raw string.

    Falling back rather than raising matters: a provider can grant a scope we never asked for
    (Slack adds implied ones), and a connection page that 500s because of unfamiliar copy would be
    a far worse failure than showing the raw string for one line.
    """
    return SCOPE_LABELS.get(scope, scope)


def listing() -> list[dict]:
    """Every known provider, flagged with whether this deployment can actually run its flow."""
    return [
        {
            "service": p.service,
            "display_name": p.display_name,
            "category": p.category,
            "summary": p.summary,
            # capability -> the scopes it needs, each already in plain English, so the marketplace
            # can show what a Connect will ask for BEFORE the user is bounced to a consent screen.
            "scope_detail": {
                cap: [{"scope": sc, "label": scope_label(sc)} for sc in scopes]
                for cap, scopes in sorted(p.scopes.items())
            },
            "capabilities": p.capabilities,
            "default_capability": p.default_capability,
            "resource_label": p.resource_label,
            "resource_plural": p.resource_plural,
            "supports_discovery": p.supports_discovery,
            "has_identity": p.has_identity,
            "auth_kind": p.auth_kind,
            "token_label": p.token_label,
            "token_placeholder": p.token_placeholder,
            "setup_url": p.setup_url,
            "setup_action_label": p.setup_action_label,
            "setup_steps": list(p.setup_steps),
            "setup_note": p.setup_note,
            "extra_credential_note": p.extra_credential_note,
            "extra_credential_label": p.extra_credential_label,
            "needs_extra_credential": p.needs_extra_credential,
            "base_url": p.base_url,
            "docs_url": p.docs_url,
            "configured": is_configured(p),
        }
        # Grouped first, alphabetical within a shelf — so the dashboard can render the shelves by
        # walking the list once instead of re-sorting what the registry already knows.
        for p in sorted(
            REGISTRY.values(),
            key=lambda p: (
                CATEGORY_ORDER.index(p.category) if p.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
                p.display_name,
            ),
        )
    ]
