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
    # Shown on the provider page and in the capability modal, i.e. everywhere Connect can be
    # clicked, BEFORE the consent popup opens. For providers whose consent screen names something
    # the user has not seen on treg: the Meta app is registered as "Crewlet", a sibling product, so
    # without this the popup asks them to authorize a stranger. Meta Developer Policy 1.6 wants the
    # relationship disclosed at the point of consent, not in a docs link.
    consent_notice: str = ""
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
    # A JSON field in the probe response that must be TRUTHY for the key to be valid — for providers
    # that answer HTTP 200 even on a BAD key and signal validity only in the body (Slack: "ok";
    # Apollo: "is_logged_in"). Empty means trust the HTTP status (most providers 401/400 a bad key).
    token_verify_field: str = ""
    # The INVERSE: reject if this JSON field is present/truthy — for providers that answer 200 with an
    # error object on a bad key (Serpstat's JSON-RPC `error`).
    token_reject_field: str = ""
    # POST-style verify probe, for providers whose key-check needs a request body (Serpstat's JSON-RPC
    # limits call). `probe_method` defaults to GET; `probe_json` is sent as the JSON body when set.
    probe_method: str = "GET"
    probe_json: dict | None = None
    # Encode the pasted secret before storing: "base64" turns a pasted `login:password` into the Base64
    # blob HTTP Basic needs (DataForSEO, Moz), so `token_format="Basic {secret}"` renders correctly and
    # the stored value injects the same way on every proxy call.
    token_encode: str = ""
    # Accept only when a JSON field EQUALS a value — for providers that answer 200 with a status
    # string (Majestic: {"Code":"OK"} on success vs {"Code":"FailedRequestViaAPI"} on a bad key).
    token_ok_field: str = ""
    token_ok_value: str = ""
    # Status codes that mean INVALID KEY specifically (default: any >=400 rejects). Coresignal has no
    # free probe, so we POST an empty body: a valid key answers 400/422 (bad request, no charge) while
    # an invalid key answers 401 — so only 401/403 should count as a bad-key rejection there.
    probe_reject_statuses: tuple[int, ...] = ()

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
        "Channel, video and playlist data with view counts and statistics. Upload and manage videos too."
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
    token_verify_field="ok",  # Slack answers 200 with {"ok": false} for a dead token
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

# The Meta app behind all three providers is registered as "Crewlet" — a sibling product from the
# same company — and Facebook's consent screen renders only that bare app name, with no parent
# business. Someone who came here for treg would be asked to authorize a product they have never
# heard of. Say so before they click Connect, not after.
_META_CONSENT_NOTICE = (
    "You'll see Crewlet on Facebook: our Meta app by Superdesign Dev Inc, shared with treg."
)

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
    consent_notice=_META_CONSENT_NOTICE,
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
    consent_notice=_META_CONSENT_NOTICE,
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
    consent_notice=_META_CONSENT_NOTICE,
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
    # Free auth check. It answers HTTP 200 even for a BAD key ({"healthy":true,"is_logged_in":false});
    # validity is the is_logged_in field, so the probe must read it, not the status (verified live).
    probe_path="/auth/health",
    token_verify_field="is_logged_in",
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
    # Trailing slash matters: /company/search (no slash) 307-redirects to /company/search/, and we
    # don't follow redirects with the key attached. Hitting the slashed path directly returns a clean
    # 401 {"detail":"Invalid API key"} for a bad key (verified live).
    probe_path="/v1/company/search/?query=canva.com",
)

RISK_DATA_API = OAuthProvider(
    service="risk-data-api",
    display_name="Risk-Data API",
    auth_kind="key",
    token_label="API key",
    token_placeholder="tnt_sk_your_key_here",
    # Rides as a standard Bearer header — both are the dataclass defaults, set explicitly here
    # for clarity next to the rest of the entry.
    token_header="Authorization",
    token_format="Bearer {secret}",
    setup_url="https://tnt-audit.com/risk-api#get-key",
    setup_action_label="Get your Risk-Data API key",
    setup_steps=(
        'Open the Risk-Data API page and enter an email under "Get your API key".',
        "The key is issued instantly — no card, no approval wait, one key per email.",
    ),
    setup_note="15 requests/day free per key; pay-per-call ($0.07, $0.03 once subscribed) past that.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Crypto/Safety",
    summary="Safety score and live insider-wallet-cluster detection for any Solana token mint — the risk check an agent runs before it swaps or pays.",
    base_url="https://tnt-audit.com/api/v1",
    docs_url="https://tnt-audit.com/risk-api",
    # GET /billing/status is the one route in this API that is genuinely free — it reads the
    # caller's own key record and deliberately skips enforceRateLimit(), so probing a connection
    # here never spends any of the 15/day free quota. token-risk itself would also work as a probe
    # (401 on a bad key, same as this one) but would burn the very quota a "check the key" ping
    # shouldn't cost.
    probe_path="/billing/status",
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
    # Account status: free, answers 200 for a valid token and 401 "Invalid credentials" for a bad one
    # (verified live 2026-08-13). The old inferred /datasets/v3/datasets was not a real route — it
    # 404'd "Cannot GET" even with a valid token, so every real key was refused at connect.
    probe_path="/status",
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

CRUNCHBASE = OAuthProvider(
    service="crunchbase",
    display_name="Crunchbase",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Crunchbase user key",
    token_header="X-cb-user-key",
    token_format="{secret}",
    setup_url="https://data.crunchbase.com/docs/using-the-api",
    setup_action_label="Find your Crunchbase API key",
    setup_steps=(
        "Crunchbase issues API keys with an Enterprise/Applications license — a Team Owner finds it "
        "under Integrations settings.",
        "Paste it here.",
    ),
    setup_note="Requires a paid Crunchbase API license; keys are not self-service.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Company, funding, acquisition and people data from Crunchbase.",
    base_url="https://api.crunchbase.com/v4/data",
    docs_url="https://data.crunchbase.com/docs/using-the-api",
    probe_path="/autocompletes?query=google&limit=1",  # rate-limited, no per-call credit
)

JUSTONEAPI = OAuthProvider(
    service="justoneapi",
    display_name="Just One API",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Just One API token",
    token_location="query",  # token rides as ?token=…, not a header
    token_param="token",
    token_format="{secret}",
    setup_url="https://dashboard.justoneapi.com/en",
    setup_action_label="Get your Just One API token",
    setup_steps=(
        "Sign in to the Just One API dashboard and open Token management.",
        "Copy your token.",
    ),
    setup_note="Data calls are billed only on success (code 0); errors and empty results are free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Social media",
    summary="A second social-scraping backend — TikTok, Instagram, YouTube, X, Weibo, Xiaohongshu and more.",
    base_url="https://api.justoneapi.com",
    docs_url="https://docs.justoneapi.com/en/usage",
    # A bad token returns HTTP 401 {"code":100,"message":"TOKEN INVALID/UNACTIVATE"} (verified live), so
    # the standard status check rejects it. A valid token on this endpoint returns code:0 (~1 credit).
    # The param is camelCase `uniqueId` — with the old snake_case `unique_id` the API answered HTTP 400
    # "must input one of them (uniqueId or secUid)" and a VALID token was refused at connect
    # (verified live 2026-08-13).
    probe_path="/api/tiktok/get-user-detail/v1?uniqueId=tiktok",
)

SCRAPECREATORS = OAuthProvider(
    service="scrapecreators",
    display_name="ScrapeCreators",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your ScrapeCreators API key",
    token_header="x-api-key",
    token_format="{secret}",
    setup_url="https://scrapecreators.com",
    setup_action_label="Get your ScrapeCreators API key",
    setup_steps=(
        "Sign up at scrapecreators.com and open the dashboard.",
        "Copy your API key.",
    ),
    setup_note="Data calls are billed per credit; the credit-balance check is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Social media",
    summary="Scrape public profiles, posts and ads across TikTok, Instagram, YouTube, LinkedIn, Reddit and 15+ platforms.",
    base_url="https://api.scrapecreators.com",
    docs_url="https://docs.scrapecreators.com",
    # Free credit check. It answers HTTP 200 even for a BAD key ({"success":true,"creditCount":0}),
    # so validity is the creditCount field, not the status (verified live 2026-07-28). A real key
    # with 0 credits also fails, which is right: such a key gets 402 on every data endpoint anyway.
    probe_path="/v1/account/credit-balance",
    token_verify_field="creditCount",
)

# ---- SEO API-key providers -------------------------------------------------------------------

DATAFORSEO = OAuthProvider(
    service="dataforseo",
    display_name="DataForSEO",
    auth_kind="key",
    token_label="API login:password",
    token_placeholder="your DataForSEO login:password",
    token_header="Authorization",
    token_format="Basic {secret}",
    token_encode="base64",  # paste "login:password"; we Base64 it for HTTP Basic
    setup_url="https://app.dataforseo.com/api-access",
    setup_action_label="Get your DataForSEO API credentials",
    setup_steps=(
        "Sign in to DataForSEO and open API access.",
        "Copy your API login and the SEPARATE API password (not your account password).",
        "Paste them here as login:password.",
    ),
    setup_note="Use the API password from the dashboard, not your account login password.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="SERP, keyword, backlink, on-page and traffic data across search engines.",
    base_url="https://api.dataforseo.com/v3",
    docs_url="https://docs.dataforseo.com/v3/auth/",
    probe_path="/appendix/user_data",  # free account info (no credits)
)

SERANKING = OAuthProvider(
    service="seranking",
    display_name="SE Ranking",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your SE Ranking API key",
    token_header="Authorization",
    token_format="Token {secret}",  # the word "Token", not "Bearer"
    setup_url="https://seranking.com/api/how-to-get-api/",
    setup_action_label="Get your SE Ranking API key",
    setup_steps=(
        "Sign in to SE Ranking and open the API section.",
        "Create an API key and copy it.",
    ),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Rank tracking, keyword research, backlinks and competitor data.",
    base_url="https://api.seranking.com",
    docs_url="https://seranking.com/api/data/getting-started/",
    probe_path="/v1/account/subscription",  # free key check + plan
)

MOZ = OAuthProvider(
    service="moz",
    display_name="Moz",
    auth_kind="key",
    token_label="AccessID:SecretKey",
    token_placeholder="your Moz AccessID:SecretKey",
    token_header="Authorization",
    token_format="Basic {secret}",
    token_encode="base64",  # paste "AccessID:SecretKey"; Base64 for HTTP Basic
    setup_url="https://moz.com/api/dashboard",
    setup_action_label="Get your Moz API credentials",
    setup_steps=(
        "Open the Moz API dashboard.",
        "Copy your Access ID and Secret Key.",
        "Paste them here as AccessID:SecretKey.",
    ),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Domain Authority, Page Authority, backlinks and link metrics.",
    base_url="https://lsapi.seomoz.com/v2",
    docs_url="https://moz.com/api/docs",
    # POST /usage_data {} is Moz's quota meter: free, and it answers 200 even when the account's row
    # quota is exhausted — so it probes key VALIDITY without ever spending or false-alarming on an
    # empty quota. (/quota, probed before 2026-07-28, is not a real V2 route: the API's own error
    # message enumerates the valid actions and /quota is absent — see src/treg/catalog/moz.yaml.)
    probe_method="POST",
    probe_path="/usage_data",
    probe_json={},
)

MAJESTIC = OAuthProvider(
    service="majestic",
    display_name="Majestic",
    auth_kind="key",
    token_label="OpenApp API key",
    token_placeholder="your Majestic app_api_key",
    token_location="query",
    token_param="app_api_key",
    token_format="{secret}",
    setup_url="https://developer-support.majestic.com/openapps/",
    setup_action_label="Get your Majestic OpenApp key",
    setup_steps=(
        "Register an OpenApp in Majestic developer settings.",
        "Copy the app_api_key.",
    ),
    setup_note="Requires a paid Majestic plan and a registered OpenApp.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Backlink metrics — Trust Flow, Citation Flow, referring domains and anchors.",
    base_url="https://api.majestic.com",
    docs_url="https://developer-support.majestic.com/api/",
    probe_path="/api/json?cmd=GetSubscriptionInfo",  # free (0 Analysis Units)
    # Answers HTTP 200 even on a bad key; validity is {"Code":"OK"} vs {"Code":"FailedRequestViaAPI"}.
    token_ok_field="Code",
    token_ok_value="OK",
)

SERPSTAT = OAuthProvider(
    service="serpstat",
    display_name="Serpstat",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Serpstat token",
    token_location="query",
    token_param="token",
    token_format="{secret}",
    setup_url="https://serpstat.com/api/660-how-to-get-create-token/",
    setup_action_label="Get your Serpstat API token",
    setup_steps=(
        "Sign in to Serpstat and open the API page.",
        "Create a token and copy it.",
    ),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Keyword research, backlinks, rank tracking and domain analytics.",
    base_url="https://api.serpstat.com/v4",
    docs_url="https://api-docs.serpstat.com/",
    # JSON-RPC over POST; a bad token answers HTTP 200 with an `error` object, so reject on that field.
    probe_method="POST",
    probe_json={"id": "1", "method": "SerpstatLimitsProcedure.getStats", "params": {}},
    token_reject_field="error",
)

# ---- more Enrichment API-key providers -------------------------------------------------------

LUSHA = OAuthProvider(
    service="lusha",
    display_name="Lusha",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Lusha API key",
    token_header="api_key",  # literally "api_key", no scheme prefix
    token_format="{secret}",
    setup_url="https://dashboard.lusha.com/api",
    setup_action_label="Get your Lusha API key",
    setup_steps=("Sign in to Lusha and open the API settings.", "Copy your API key."),
    setup_note="API access is generally on paid plans (Pro/Scale).",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Verified direct-dial and mobile phone numbers, plus person and company enrichment.",
    base_url="https://api.lusha.com",
    docs_url="https://docs.lusha.com/apis/openapi/section/authentication",
    probe_path="/v3/account/usage",  # free account snapshot
)

CORESIGNAL = OAuthProvider(
    service="coresignal",
    display_name="Coresignal",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Coresignal API key",
    token_header="apikey",  # one word, lowercase
    token_format="{secret}",
    setup_url="https://dashboard.coresignal.com/",
    setup_action_label="Get your Coresignal API key",
    setup_steps=("Sign in to the Coresignal dashboard and open API keys.", "Copy your key."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Large-scale employee, job-posting and company data via search and collect.",
    base_url="https://api.coresignal.com/cdapi/v2",
    docs_url="https://docs.coresignal.com/api-introduction/authorization",
    # No free account endpoint: POST an empty body — a valid key answers 400 (no charge), an invalid
    # key answers 401 — so only 401/403 count as a bad-key rejection (verified: bad key -> 401).
    probe_method="POST",
    probe_path="/company_base/search/es_dsl",
    probe_json={},
    probe_reject_statuses=(401, 403),
)

DIFFBOT = OAuthProvider(
    service="diffbot",
    display_name="Diffbot",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Diffbot token",
    token_location="query",  # token is a query param on every call
    token_param="token",
    token_format="{secret}",
    setup_url="https://app.diffbot.com/get-started/",
    setup_action_label="Get your Diffbot token",
    setup_steps=("Sign in to Diffbot and open your account.", "Copy your API token."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Knowledge-graph entities (organizations, people) and AI web extraction.",
    # Enhance/enrich + DQL live on the KG host; the free account probe lives on the api host, so verify
    # off-host. The provisioned tool points at the KG host (the enrichment value).
    base_url="https://kg.diffbot.com/kg/v3",
    docs_url="https://docs.diffbot.com/reference/authentication",
    probe_url="https://api.diffbot.com/v4/account",  # token injected as ?token=…
)

THECOMPANIESAPI = OAuthProvider(
    service="thecompaniesapi",
    display_name="The Companies API",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Companies API token",
    token_header="Authorization",
    token_format="Basic {secret}",  # the RAW token after "Basic ", NOT base64 (no token_encode)
    setup_url="https://www.thecompaniesapi.com/",
    setup_action_label="Get your Companies API token",
    setup_steps=("Sign up at thecompaniesapi.com.", "Copy your API token from the dashboard."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Firmographics and technology-stack detection across ~50M companies.",
    base_url="https://api.thecompaniesapi.com/v2",
    docs_url="https://www.thecompaniesapi.com/api/authentication",
    probe_path="/companies?simplified=true&size=1",  # free (no credits), auth required
)

LEADMAGIC = OAuthProvider(
    service="leadmagic",
    display_name="LeadMagic",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your LeadMagic API key",
    token_header="X-API-Key",
    token_format="{secret}",
    setup_url="https://app.leadmagic.io/dashboard/api-keys",
    setup_action_label="Get your LeadMagic API key",
    setup_steps=("Sign in to LeadMagic and open API keys.", "Copy your key."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Enrichment",
    summary="Find and verify emails and mobile numbers, and enrich people and companies.",
    base_url="https://api.leadmagic.io",
    docs_url="https://leadmagic.io/docs/v1/authentication",
    probe_path="/v1/credits",  # free — no credits consumed
)

# ---- Advertising API-key providers (ad intelligence) -----------------------------------------

SPYFU = OAuthProvider(
    service="spyfu",
    display_name="SpyFu",
    auth_kind="key",
    token_label="Secret key",
    token_placeholder="your SpyFu secret key",
    token_location="query",  # ?api_key=<SecretKey> (the secret alone; API id not needed for this form)
    token_param="api_key",
    token_format="{secret}",
    setup_url="https://www.spyfu.com/account/api",
    setup_action_label="Get your SpyFu API key",
    setup_steps=("Open SpyFu Account settings → API.", "Copy your Secret Key."),
    # The SpyFu dashboard shows THREE credentials (API ID, Secret Key, Base64 Key); only the short
    # Secret Key works here, so the note has to name the other two or users paste them and get a 401.
    setup_note="Paste ONLY the short Secret Key (e.g. DR…) — not the API ID and not the Base64 key.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Advertising",
    summary="Competitor search-ad keywords, estimated ad spend and PPC history.",
    base_url="https://api.spyfu.com",
    docs_url="https://developer.spyfu.com/",
    probe_path="/apis/domain_stats_api/v2/getAllDomainStats?domain=spyfu.com",
)

APIFY = OAuthProvider(
    service="apify",
    display_name="Apify",
    auth_kind="key",
    token_label="API token",
    token_placeholder="your Apify API token",
    token_header="Authorization",
    token_format="Bearer {secret}",
    setup_url="https://console.apify.com/settings/integrations",
    setup_action_label="Get your Apify API token",
    setup_steps=("Open Apify Console → Settings → Integrations.", "Copy your API token."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Advertising",
    summary="Run Facebook and TikTok ad-library scraper actors for ad creatives and targeting.",
    base_url="https://api.apify.com/v2",
    docs_url="https://docs.apify.com/api/v2",
    probe_path="/users/me",  # free; 401 on bad token
)

META_AD_LIBRARY = OAuthProvider(
    service="meta-ad-library",
    display_name="Meta Ad Library",
    auth_kind="key",
    token_label="Access token",
    token_placeholder="your Meta ad-library access token",
    token_location="query",
    token_param="access_token",
    token_format="{secret}",
    setup_url="https://www.facebook.com/ads/library/api/",
    setup_action_label="Set up Meta Ad Library API access",
    setup_steps=(
        "Confirm your identity at facebook.com/ID (upload a government ID; 1–3 days).",
        "Create a Meta app and generate an access token.",
    ),
    setup_note="Official free competitive ad data; requires one-time Meta identity verification.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Advertising",
    summary="Official Meta ad library — anyone's live Facebook/Instagram ads and spend ranges.",
    base_url="https://graph.facebook.com/v21.0",
    docs_url="https://www.facebook.com/ads/library/api/",
    # Required params baked in (ad_reached_countries is a URL-encoded JSON array); access_token injected.
    probe_path='/ads_archive?search_terms=coffee&ad_reached_countries=%5B%22US%22%5D&limit=1&fields=id',
)

SERPAPI = OAuthProvider(
    service="serpapi",
    display_name="SerpApi",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your SerpApi key",
    token_location="query",
    token_param="api_key",
    token_format="{secret}",
    setup_url="https://serpapi.com/manage-api-key",
    setup_action_label="Get your SerpApi key",
    setup_steps=("Sign in to SerpApi.", "Copy your API key from the dashboard."),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Advertising",
    summary="Google search, Shopping and paid-ad results from live SERPs.",
    base_url="https://serpapi.com",
    docs_url="https://serpapi.com/search-api",
    probe_path="/account",  # free account/plan snapshot; 401 on bad key
)

# ---- Advertising OAuth platforms (UNCONFIGURED until this deployment registers a dev app) ------
# These list as `configured: false` until treg holds each platform's client id/secret. Microsoft +
# Snapchat fit the existing OAuth machinery (Microsoft additionally needs the user's own developer
# token, like Google Ads). TikTok Ads is NON-STANDARD OAuth and needs oauth.py + binding work before
# it can actually run — it is a registry placeholder for now (see the comment on it).

MICROSOFT_ADS = OAuthProvider(
    service="microsoft-ads",
    display_name="Microsoft Advertising",
    auth_uri="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_uri="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    # One combined read+write scope; offline_access buys a refresh token, openid the identity.
    scopes={"manage": ["https://ads.microsoft.com/msads.manage", "offline_access", "openid"]},
    client_id_setting="microsoft_ads_client_id",
    client_secret_setting="microsoft_ads_client_secret",
    category="Advertising",
    summary="Run and report on your Microsoft (Bing) search-ad campaigns.",
    base_url="https://campaign.api.bingads.microsoft.com/CampaignManagement/v13",
    docs_url="https://learn.microsoft.com/en-us/advertising/guides/get-started",
    token_endpoint_auth_method="client_secret_post",
    auth_params={},  # Microsoft identity v2.0; don't send Google's access_type/prompt
    # Bing REST needs a DeveloperToken header on every call besides the OAuth bearer — issued
    # instantly for first-party use, so the user supplies it (like Google Ads' developer token).
    extra_credential_note=(
        "Microsoft Advertising needs a developer token (issued instantly in the Microsoft Advertising "
        "Developer Portal). Add it under Secrets and bind it as a DeveloperToken header."
    ),
    extra_credential_label="Developer token",
    extra_credential_header="DeveloperToken",
)

SNAPCHAT_ADS = OAuthProvider(
    service="snapchat-ads",
    display_name="Snapchat Ads",
    auth_uri="https://accounts.snapchat.com/login/oauth2/authorize",
    token_uri="https://accounts.snapchat.com/login/oauth2/access_token",
    scopes={"manage": ["snapchat-marketing-api"]},
    client_id_setting="snapchat_ads_client_id",
    client_secret_setting="snapchat_ads_client_secret",
    category="Advertising",
    summary="Run and report on your Snapchat ad campaigns.",
    base_url="https://adsapi.snapchat.com/v1",
    docs_url="https://developers.snap.com/marketing-api/Ads-API/authentication",
    token_endpoint_auth_method="client_secret_post",
    auth_params={},
    probe_path="/me",  # cheap token check once configured; auto-provisions a Bearer tool
)

# TikTok Ads is NON-STANDARD OAuth: the token exchange uses app_id/secret + auth_code in a JSON body
# and returns a {"code":0,"data":{...}} envelope, and API calls authenticate with an `Access-Token`
# header (NOT `Authorization: Bearer`). oauth.py (standard code/grant_type exchange) and the Bearer
# auto-provision binding do NOT handle this yet, so this entry cannot be configured to work until that
# code is added. Kept as a placeholder so the platform is visible on the shelf. See notes.
TIKTOK_ADS = OAuthProvider(
    service="tiktok-ads",
    display_name="TikTok Ads",
    auth_uri="https://business-api.tiktok.com/portal/auth",
    token_uri="https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/",
    scopes={"manage": ["ads.management"]},  # nominal; TikTok's real scopes are numeric ids set in the portal
    client_id_setting="tiktok_ads_client_id",
    client_secret_setting="tiktok_ads_client_secret",
    category="Advertising",
    summary="Run and report on your TikTok ad campaigns.",
    base_url="https://business-api.tiktok.com/open_api/v1.3",
    docs_url="https://business-api.tiktok.com/portal/docs",
    token_endpoint_auth_method="client_secret_post",
    client_id_param="app_id",  # TikTok spells the client id "app_id"
    auth_params={},
)

PINTEREST_ADS = OAuthProvider(
    service="pinterest-ads",
    display_name="Pinterest Ads",
    auth_uri="https://www.pinterest.com/oauth/",
    token_uri="https://api.pinterest.com/v5/oauth/token",
    scopes={
        "read": ["ads:read", "user_accounts:read"],
        "manage": ["ads:read", "ads:write", "user_accounts:read"],
    },
    client_id_setting="pinterest_client_id",
    client_secret_setting="pinterest_client_secret",
    category="Advertising",
    summary="Run and report on your Pinterest ad campaigns.",
    base_url="https://api.pinterest.com/v5",
    docs_url="https://developers.pinterest.com/docs/api/v5/",
    token_endpoint_auth_method="client_secret_basic",  # Pinterest posts Basic client auth
    auth_params={},
    probe_path="/user_account",  # cheap token check once configured; auto-provisions a Bearer tool
)

REGISTRY: dict[str, OAuthProvider] = {
    p.service: p
    for p in (
        GOOGLE_SEARCH_CONSOLE, GOOGLE_ANALYTICS, GOOGLE_BUSINESS_PROFILE, GOOGLE_ADS, YOUTUBE,
        LINKEDIN, SLACK, X, TIKTOK, FACEBOOK, INSTAGRAM, META_ADS,
        # API-key providers
        APOLLO, PDL, AKTA, HUNTER, CRUNCHBASE, TIKHUB, BRIGHTDATA, SEMRUSH, JUSTONEAPI,
        SCRAPECREATORS,
        # Crypto/Safety API-key providers
        RISK_DATA_API,
        # SEO API-key providers
        DATAFORSEO, SERANKING, MOZ, MAJESTIC, SERPSTAT,
        # more Enrichment API-key providers
        LUSHA, CORESIGNAL, DIFFBOT, THECOMPANIESAPI, LEADMAGIC,
        # Advertising: API-key ad intelligence + unconfigured OAuth ad platforms
        SPYFU, APIFY, META_AD_LIBRARY, SERPAPI,
        MICROSOFT_ADS, SNAPCHAT_ADS, TIKTOK_ADS, PINTEREST_ADS,
    )
}

DEFAULT_CAPABILITY = "read"

# Shelf order in the marketplace. Anything carrying a category not named here sorts last, so a
# provider added without one is visible rather than lost between the shelves.
CATEGORY_ORDER = ("SEO", "Advertising", "Social media", "Enrichment", "Crypto/Safety", "Community", "Other")


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
    # Microsoft Advertising
    "https://ads.microsoft.com/msads.manage": "Manage your Microsoft Advertising campaigns and reports",
    "offline_access": "Stay connected without asking you to sign in again",
    # Snapchat Ads
    "snapchat-marketing-api": "Manage your Snapchat ad campaigns and reporting",
    # TikTok Ads (nominal — real scopes are numeric ids configured in the TikTok portal)
    "ads.management": "Manage your TikTok ad campaigns and reporting",
    # Pinterest Ads
    "ads:read": "Read your ad accounts, campaigns and performance",
    "ads:write": "Create and change your ad campaigns",
    "user_accounts:read": "See which ad accounts you have access to",
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
            "consent_notice": p.consent_notice,
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
