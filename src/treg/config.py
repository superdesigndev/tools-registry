"""Settings — read once from env/.env. Keep it tiny and explicit."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# Every hostname the reference deployment has EVER answered to. treg moved
# treg.superdesign.dev → treg.to (2026-08); installed CLIs, skill.md files, .mcp.json configs and
# MCP OAuth grants exist against BOTH names, so both stay valid everywhere a host or audience is
# recognized — mcp.py's transport allow-lists, mcp_oauth's token audiences, api.py's login-callback
# anchoring — REGARDLESS of which one `public_url` currently points at. That symmetry is what makes
# a TREG_PUBLIC_URL revert a complete rollback: grants and logins minted on either name survive the
# flip in either direction. Self-hosters are unaffected: these only ADD accepted names, and none of
# them resolve to a self-hosted deployment.
PUBLIC_HOST_ALIASES: tuple[str, ...] = ("treg.superdesign.dev", "treg.to")

# The subset browsers are REDIRECTED AWAY FROM (marketing pages only; see api.py's middleware).
# Deliberately one-way — only ever the pre-move name, never treg.to — so a browser that cached the
# old→new 301 can never meet a new→old redirect and loop, even while a rollback is in effect.
LEGACY_PUBLIC_HOSTS: tuple[str, ...] = ("treg.superdesign.dev",)


def platform_setting_name(provider: str) -> str:
    """The Settings attribute holding treg's own key for `provider` — the string a `platform_setting`
    binding carries, and the only form of a platform credential that ever leaves this module."""
    return "platform_key_" + (provider or "").lower().replace("-", "_")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TREG_", extra="ignore")

    # SQLite locally, Postgres on Render — same code path, just swap the URL.
    database_url: str = "sqlite+aiosqlite:///./treg.db"

    @field_validator("database_url")
    @classmethod
    def _async_pg_driver(cls, v: str) -> str:
        # Render's `fromDatabase` (render.yaml) injects a bare `postgres://`/`postgresql://` URL, but
        # our async engine (create_async_engine) needs the asyncpg driver. Rewrite the scheme so the
        # Blueprint can auto-wire the DB with no manual URL editing. No-op for sqlite / already-drivered URLs.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v

    # Fernet key (urlsafe base64, 32 bytes). Generate with `treg keygen` (see crypto.py).
    # Empty in dev means an ephemeral key is minted at startup (secrets won't survive a restart).
    secret_key: str = ""

    # The single bootstrap caller token for the MVP. Per-user/org tokens come in Step 3.
    api_token: str = "dev-token"

    # DEMO Stripe webhook signing secret (env TREG_DEMO_STRIPE_WEBHOOK_SECRET) for the landing page's live
    # payments feed (see pubfeed.py). Empty = the /stripe/webhook endpoint is off (404).
    demo_stripe_webhook_secret: str = ""

    # The Stripe sandbox restricted key (env TREG_DEMO_STRIPE_KEY) behind the landing sandbox's ONE
    # live wire: a sandbox call to the exact seeded stripe tool relays for real with THIS key
    # injected — the key never exists in any sandbox org (see sandbox.is_live_tool / api.call_tool).
    # Empty = every sandbox call synthesizes, exactly as before the live wire existed.
    demo_stripe_key: str = ""

    # Cross-tenant super-admin bearer (env TREG_ADMIN_TOKEN). Presenting it authorizes every
    # /admin/* endpoint regardless of org. Empty = the env key is disabled (only is_superadmin
    # users can reach /admin). Keep it long + secret; it sees ALL orgs.
    admin_token: str = ""

    # Isolated-runner proof for `treg run --local` (env TREG_RUN_PROOF). A grant that would return a
    # secret the CALLER does NOT own (a shared-key tool a member may run but not read) requires this
    # value in the `X-Treg-Run-Proof` header — held ONLY by the root-installed treg-run runner, never
    # by the member. Empty = shared-key local runs are refused (owned-secret runs still work). Set it
    # on the server AND install it via `treg setup-local-run --run-proof` to enable shared local runs.
    run_proof: str = ""

    # `treg run --server` allow-list. The server only executes an entrypoint that is a catalog-known CLI
    # (stripe/gh/vercel/…) OR listed here (comma-separated) — so a member can't name `bash`/`python` and
    # run arbitrary code as the server user. Extend it as new CLIs are approved. (Full filesystem/network
    # isolation — the stronger fix — needs a container deploy and is a planned follow-up.)
    run_allowed_bins: str = ""

    # Server-run resource limits (the DoS half of the server-run sandbox). Every `--server` run's child
    # process gets these POSIX rlimits so a runaway or hostile CLI can't exhaust the host. On by default;
    # a no-op where `resource` is unavailable (non-Unix). We deliberately do NOT cap address space or
    # process count — a virtual-memory cap crashes Go-based CLIs (gh/stripe/doctl), and the per-user
    # process cap is shared with the server itself. CPU-seconds + max-file-size + no-core-dumps are the
    # safe, high-value guards. Set TREG_RUN_RLIMITS=false to disable entirely.
    run_rlimits: bool = True
    run_cpu_seconds: int = 300          # CPU time a single server run may burn (backstop to the wall timeout)
    run_fsize_mb: int = 100             # largest single file a server run may write (disk-fill guard)

    # ---- prepaid balance (ledger.py) -----------------------------------------------------------
    # Platform markup on a call served by a PLATFORM key, applied to the estimate at reserve time and
    # to the observed cost at settle time. 0.0 = we charge exactly what the provider charges us.
    # It lives in config (rather than being hardcoded) so turning margin on is a deploy setting, not
    # a code change — and so the ledger records the rate that was in force for each call.
    platform_margin: float = 0.0
    # The signup gift, in micro-USD (1e-6 USD): $1 buys ~1,600 catalog calls, enough for an agent to
    # get real work done before it ever sees a payment form. Granted once, at org creation only.
    promo_grant_micro: int = 1_000_000
    # Upstream HTTP timeout for a relayed call (the shared httpx client). Also the base of the hold
    # reaper's cutoff: a hold older than call_timeout_s + hold_grace_s belongs to a call that can no
    # longer be settling, so the reaper returns it (see ledger.reap_stale_holds).
    # 180, not 30: real catalog upstreams routinely run long — BrightData sync scrapes ~20-35s
    # (a 30s ceiling 502'd one live), and merchant routes have been observed at 9s→105s.
    call_timeout_s: int = 180
    hold_grace_s: int = 60

    # ---- referral program (referrals.py) --------------------------------------------------------
    # Flat bounties, not a percentage of top-ups. At 0% platform margin a percentage would be a
    # permanent share of pass-through GMV, and — unlike a flat figure — it rewards farming in
    # proportion to effort. Nobody builds a fake-account farm for $10; plenty would for 15% of an
    # uncapped balance. All figures are micro-USD (1e-6 USD), like every other amount in the system.
    # Symmetric on purpose: both sides get the same, so the offer is one sentence to explain and
    # neither party can feel like the other got the better end of it.
    referral_referrer_micro: int = 5_000_000    # $5 to the person who shared the link
    referral_referred_micro: int = 5_000_000    # $5 to the friend who signed up
    # The friend's first top-up must clear this for anything to be owed. Deliberately well above the
    # bounties themselves: below it, buying the bonus with your own card is profitable.
    referral_min_topup_micro: int = 10_000_000  # $10
    # Days between qualifying and the credit landing. This is the ONLY clawback window that exists:
    # referral credit is promotional, burns first (ledger._KIND_ORDER) and is typically spent within
    # days, so once granted it cannot be recovered. Short on purpose — a referral program that pays
    # in 60 days does not feel like a reward, and the exposure at these amounts is tiny.
    referral_hold_days: int = 7
    # Lifetime paid referrals per person. Anyone who wants more is an influencer, and that is a
    # conversation with a contract and a bank transfer — not a self-serve link. The refusal IS the
    # commercial conversation, the same posture as raising a daily cap.
    referral_cap: int = 20

    # ---- tier-4 platform keys (api.py credential ladder) ---------------------------------------
    # treg's OWN provider keys, spent on a caller's behalf and metered against their prepaid balance.
    # Read by `proxy.relay` through a `platform_setting` binding (never copied into an org's secrets,
    # never reachable from a local run). Naming is load-bearing: the binding names the ATTRIBUTE, so
    # `platform_key_for("tikhub")` → `platform_key_tikhub` → env `TREG_PLATFORM_KEY_TIKHUB`.
    # dataforseo's value is the base64 of "login:password" (HTTP Basic) — the same bytes a pasted
    # secret ends up as; the other two are the raw key.
    platform_key_tikhub: str = ""
    platform_key_dataforseo: str = ""
    platform_key_scrapecreators: str = ""
    platform_key_brightdata: str = ""
    platform_key_justoneapi: str = ""
    platform_key_serpapi: str = ""
    platform_key_moz: str = ""          # base64 of "access_id:secret_key" (HTTP Basic)
    platform_key_seranking: str = ""
    platform_key_hunter: str = ""
    platform_key_leadmagic: str = ""
    platform_key_lusha: str = ""
    platform_key_pdl: str = ""
    platform_key_diffbot: str = ""
    platform_key_akta: str = ""
    platform_key_apify: str = ""
    platform_key_serpstat: str = ""
    platform_key_spyfu: str = ""    # the SpyFu *secret key* alone (?api_key=…), not the id or base64 pair
    platform_key_coresignal: str = ""
    platform_key_thecompaniesapi: str = ""  # raw token — injected as "Basic {secret}" un-encoded
    platform_key_apollo: str = ""   # raw key (X-Api-Key); billed at the Basic-plan $/credit rate in fx.yaml
    # ---- Market data (2026-08-15). Two PAID plans billed per call, three FREE-tier keys serving
    # capped trial pools at $0 (fx.yaml kind: treg_trial — api._enforce_trial_allowance is the brake).
    platform_key_coingecko: str = ""    # PRO key (x-cg-pro-api-key); Basic $29/mo, $0.00029/credit
    platform_key_marketstack: str = ""  # access_key; Basic $9.99/mo, $0.000999/call vs the 10k/mo cap
    platform_key_finnhub: str = ""      # FREE-tier key — trial pool, 50 calls/team/day
    platform_key_twelvedata: str = ""   # FREE Basic key (800/day TOTAL) — trial pool, 20 calls/team/day
    platform_key_tiingo: str = ""       # FREE Starter key (1,000/day total) — trial pool, 20 calls/team/day
    # ---- Enrichment expansion (2026-08-20). Slots only — fund the accounts and set the keys before
    # naming any of these in TREG_PLATFORM_PROVIDERS.
    platform_key_companyenrich: str = ""  # Bearer key
    platform_key_oceanio: str = ""        # X-Api-Token; fx.yaml usd is null so tier 4 stays refused until priced
    platform_key_predictleads: str = ""   # base64 of "api_key:api_token" (HTTP Basic, like dataforseo)
    platform_key_findymail: str = ""      # Bearer key
    platform_key_branddev: str = ""       # Bearer key
    platform_key_icypeas: str = ""        # raw key (Authorization, no Bearer)
    platform_key_leadsforge: str = ""     # Bearer key
    platform_key_fiber_ai: str = ""       # Fiber AI key (x-api-key header, sk_live_…)
    platform_key_tomba: str = ""          # the API key (ta_…); X-Tomba-Key header
    platform_key_tomba_secret: str = ""   # the API secret (ts_…); X-Tomba-Secret — BOTH must be set
    # (tomba's data routes need the header pair; TOMBA.platform_extra_setting names this second slot)
    platform_key_influencersclub: str = ""  # Bearer key (dashboard JWT); creator discovery + enrichment, fx.yaml $0.598/credit (our $299/500 plan)
    # The KILL SWITCH, and the reason a key alone isn't enough: a provider serves tier 4 only if it is
    # named here AND its key is set. Empty (the default) = tier 4 is entirely off, so a deploy that
    # happens to hold a key can't start spending it by accident. `TREG_PLATFORM_PROVIDERS=""` in the
    # Render dashboard turns the whole feature off without a redeploy.
    platform_providers: str = ""
    # Per-org, per-UTC-day ceiling on tier-4 spend, and the CEILING a team may raise its own
    # `Org.daily_cap_micro` to. Enforced FAIL-CLOSED (unlike the soft per-user call cap): a query
    # error refuses the call rather than letting an unbounded amount of our money out. It is a
    # blast-radius limit on a runaway agent or a mispriced catalog entry, not a billing control —
    # the balance is what a team actually spends against.
    platform_daily_cap_usd: float = 100.0
    # OAuth providers whose UPSTREAM bill lands on treg's developer app rather than the connected
    # user (X moved to pay-per-use in Feb 2026: the app owner is billed per resource read / per post
    # written, whoever's token made the call). Calls through a registry connect of a provider named
    # here are metered against the org's balance — the same reserve→settle path as tier 4. Same
    # kill-switch shape as `platform_providers`: empty (the default) = those calls stay free, so a
    # deploy must OPT IN to charging (`TREG_OAUTH_BILLED_PROVIDERS=x`). BYO-app connections
    # (/oauth/start with the caller's own client_id) are never metered — their upstream bill is
    # already theirs.
    oauth_billed_providers: str = ""

    # ---- Stripe top-ups (billing.py) -----------------------------------------------------------
    # OUR billing account's keys. Deliberately NOT the `demo_stripe_*` pair above: that one belongs to
    # the landing page's sandbox feed and its webhook secret signs a different endpoint. Empty secret
    # key = top-ups are off (the /billing/* endpoints 503); empty webhook secret = the webhook 404s,
    # so an unconfigured deploy exposes no unauthenticated POST surface (same posture as the demo).
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # Top-up amounts, in whole USD (the ONLY place dollars appear — billing.py converts to micro-USD
    # for the ledger and to integer cents for Stripe at the boundaries, and nothing in between sees a
    # float). $5 minimum is fee math, not policy: at 2.9% + $0.30 a $1 top-up loses 33% to fees.
    topup_min_usd: int = 5
    topup_default_usd: int = 10
    topup_presets: list[int] = [5, 10, 25, 50]
    # Auto-top-up defaults, applied when an org enables it without naming its own numbers. The
    # threshold is deliberately above the $1 promo grant's tail: at agent call rates a $2 floor is one
    # burst away from empty, and an off-session charge takes seconds to land.
    autotopup_default_threshold_usd: int = 5
    autotopup_default_amount_usd: int = 10
    # Hard guardrails on the off-session charge — the difference between "convenient" and "a runaway
    # agent bills a card all night". Cap is per calendar month, cooldown is between attempts, and
    # max_attempts counts CONSECUTIVE failures before auto-top-up disables itself.
    autotopup_monthly_cap_usd: int = 100
    autotopup_cooldown_s: int = 3600
    autotopup_max_attempts: int = 3

    # Call-time SSRF guard on the proxy: resolve the upstream host and refuse an internal target. On by
    # default; the test suite disables it (its upstream is an in-process ASGI transport, not real DNS).
    proxy_ssrf_check: bool = True

    # treg's own public base URL — used to build the OAuth callback (must be whitelisted in the
    # provider's OAuth app). Self-hosting? Set TREG_PUBLIC_URL to your deployment's URL.
    public_url: str = "https://treg.to"
    # Proof to OpenAI's plugin directory that we control this domain. The portal generates a token
    # and fetches it from /.well-known/openai-apps-challenge; that endpoint must return THAT token
    # and nothing else — not JSON, not a list. Empty (the default) leaves the route 404, which is the
    # right answer for every deployment that is not ours.
    openai_apps_challenge: str = ""

    # Human login via GitHub OAuth (dashboard sessions). Create a GitHub OAuth App with callback
    # <public_url>/auth/github/callback and set these; empty disables the GitHub button.
    github_client_id: str = ""
    github_client_secret: str = ""
    # Signs the session cookie (HMAC). Falls back to secret_key if unset. Set a real value in prod.
    session_secret: str = ""
    # Overridable for tests; real GitHub by default.
    github_authorize_url: str = "https://github.com/login/oauth/authorize"
    github_token_url: str = "https://github.com/login/oauth/access_token"
    github_api_url: str = "https://api.github.com"

    # Human login via Google OAuth (dashboard sessions). Create a Google "web" OAuth client with an
    # authorized redirect of <public_url>/auth/google/callback; empty disables the Google button.
    # The SAME client also backs registry connects (oauth_providers.py) — Search Console, Analytics,
    # Ads, Business Profile — which consent through <public_url>/oauth/callback. Register both
    # redirect URIs on it. Login asks for openid/email/profile; a connect asks only for the scopes
    # its capability needs, so the two never share a consent screen.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Product analytics (dashboard onboarding + Try-it). Empty key = OFF, so self-hosted treg instances
    # send nothing — only a deployment that sets TREG_POSTHOG_KEY reports. The key is a PUBLIC posthog
    # ingestion key (safe to expose to the browser); host defaults to EU cloud.
    posthog_key: str = ""
    posthog_host: str = "https://eu.i.posthog.com"
    # Intercom Messenger (support chat; treg's own workspace). Empty app_id = OFF, so self-hosted
    # instances never load the widget. The app_id is public (visible in page source); the secret
    # signs user_hash for identity verification and must never reach the browser.
    intercom_app_id: str = ""
    intercom_secret: str = ""
    # Registry OAuth apps for the non-Google providers (oauth_providers.py). Empty = that provider
    # is listed as unconfigured rather than failing part-way through a consent.
    # treg's own Google Ads developer token, from OUR approved manager account. Ads needs it on
    # every call ALONGSIDE the user's OAuth — it identifies the calling application, not the user,
    # and grants no access to our ad accounts. Holding it centrally is the whole point: a user
    # would otherwise wait weeks for Google to approve one of their own.
    google_ads_developer_token: str = ""
    # Google Ads consents through a DEDICATED OAuth client in its own Cloud project — NOT the shared
    # google_client_id. A developer token is permanently welded to the first Cloud project it calls
    # from, and the shared project is already paired to a different token, so Ads must use a client
    # from the project its live token is paired with. Empty = Ads shows as unconfigured (correct — it
    # can't work without this) rather than silently reusing the wrong client.
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    # Google Ads conversion upload. This customer id and the refresh token below are both required;
    # if either is empty the whole feature is OFF (tests stay inert and self-hosters send nothing) —
    # the same gate shape analytics.py uses for posthog_key.
    google_ads_customer_id: str = ""
    # Manager (MCC) account used to access google_ads_customer_id. Optional for direct client auth;
    # when present this is sent as login-customer-id. It cannot be inferred from Secret.resource_ref,
    # which is the TARGET client account selected in discovery, not its manager.
    google_ads_login_customer_id: str = ""
    # treg's OWN long-lived refresh token for the Data Manager conversion uploader — a PLATFORM
    # credential, obtained once, out of band, by an operator via the OAuth playground with scope
    # https://www.googleapis.com/auth/datamanager. It is exchanged against `google_ads_client_id`/
    # `_secret` above (the refresh token is issued against that client, so it must be redeemed with
    # it), never against a customer's own OAuth connection: the uploader ships treg's OWN marketing
    # conversions to treg's OWN ad account, a different purpose from a customer connecting Ads to
    # read their campaign data, and the two must not share a credential or a consent screen. Empty
    # = the whole feature is OFF. See docs/context/architecture/ads-conversions.md.
    ads_conv_refresh_token: str = ""

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""

    slack_client_id: str = ""
    slack_client_secret: str = ""
    x_client_id: str = ""
    x_client_secret: str = ""
    # TikTok issues a separate app (and separate key/secret) per sandbox, so a dev deployment
    # points at a sandbox client while production points at the reviewed one.
    tiktok_client_id: str = ""
    tiktok_client_secret: str = ""
    # ONE Meta app backs both the facebook and instagram providers. Meta App Review, business
    # verification and Tech Provider status are all scoped to the app, not to an OAuth client, so a
    # second client would isolate nothing and would need its own review from zero. Instagram
    # Business is reached through Facebook Login (graph.facebook.com), not the separate
    # Instagram-Login API on graph.instagram.com — that is a different token family entirely and
    # will reject these credentials outright.
    meta_client_id: str = ""
    meta_client_secret: str = ""
    # Advertising OAuth platforms — unset by default, so these providers list as "not configured"
    # until this deployment registers its own developer app on each network.
    microsoft_ads_client_id: str = ""
    microsoft_ads_client_secret: str = ""
    snapchat_ads_client_id: str = ""
    snapchat_ads_client_secret: str = ""
    tiktok_ads_client_id: str = ""
    tiktok_ads_client_secret: str = ""
    pinterest_client_id: str = ""
    pinterest_client_secret: str = ""

    # Overridable for tests; real Google by default.
    google_authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"

    # Email one-time-code login (the third identity door). Dev mode RETURNS the code in the API
    # response, which is an unauthenticated account-takeover vector in prod — so it defaults OFF and
    # must be explicitly enabled (TREG_EMAIL_DEV_MODE=true) for local testing without a mail sender.
    email_dev_mode: bool = False

    # Frictionless local mode: `curl … | sh` brings up a server you are already signed into, with no
    # account, email or password. Only takes effect when `single_user_ok` allows it (see below).
    single_user: bool = False
    # Where the bootstrapped token is written so the CLI/installer can pick it up (0600).
    single_user_token_file: str = "~/.treg/local-token"

    @property
    def single_user_ok(self) -> bool:
        """No-login mode: ONE person on ONE machine, so the dashboard opens already signed in.

        Guarded exactly like `expose_dev_code`, because a no-login dashboard reachable from the
        internet would hand the whole registry to anyone who found it. Both must hold:
          - a LOCAL sqlite database (a Postgres URL means a real deploy), and
          - a loopback `public_url` (so it is not fronted by a public domain).
        A stray TREG_SINGLE_USER=true in production therefore does nothing.
        """
        if not self.single_user or "sqlite" not in self.database_url:
            return False
        host = (urlsplit(self.public_url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")

    @property
    def platform_provider_set(self) -> frozenset[str]:
        """The allow-listed tier-4 providers (comma-separated `TREG_PLATFORM_PROVIDERS`)."""
        return frozenset(p.strip().lower() for p in self.platform_providers.split(",") if p.strip())

    def platform_key_for(self, provider: str) -> str | None:
        """treg's own key for `provider`, or None if tier 4 must not serve it. BOTH conditions have to
        hold — the provider is allow-listed AND a key is configured — so neither half alone can start
        spending our money. Returns the value only; callers put the SETTING NAME in the binding
        (`platform_setting_name`) so the key itself never travels through a tool row."""
        if (provider or "").lower() not in self.platform_provider_set:
            return None
        return getattr(self, platform_setting_name(provider), "") or None

    @property
    def platform_daily_cap_micro(self) -> int:
        return int(round(self.platform_daily_cap_usd * 1_000_000))

    @property
    def oauth_billed_set(self) -> frozenset[str]:
        """OAuth providers whose registry-connect calls are metered (comma-separated
        `TREG_OAUTH_BILLED_PROVIDERS`). Empty = the current free behavior."""
        return frozenset(p.strip().lower() for p in self.oauth_billed_providers.split(",") if p.strip())

    @property
    def expose_dev_code(self) -> bool:
        """Dev login codes may be returned in the response ONLY on a local sqlite database — never on a
        real (Postgres) deploy. So even a stray TREG_EMAIL_DEV_MODE=true in production can't leak codes."""
        return self.email_dev_mode and "sqlite" in self.database_url

    # Transactional email via Resend (OTP sign-in codes + team invitations). Empty key = no real
    # send (dev mode still returns the code; prod without a key silently skips the send). From must
    # be a Resend-verified domain — treg.to is verified (DKIM + SPF); treg.superdesign.dev remains
    # verified as a fallback.
    resend_api_key: str = ""
    email_from: str = "tools-registry <no-reply@treg.to>"


@lru_cache
def get_settings() -> Settings:
    return Settings()
