"""Settings — read once from env/.env. Keep it tiny and explicit."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Call-time SSRF guard on the proxy: resolve the upstream host and refuse an internal target. On by
    # default; the test suite disables it (its upstream is an in-process ASGI transport, not real DNS).
    proxy_ssrf_check: bool = True

    # treg's own public base URL — used to build the OAuth callback (must be whitelisted in the
    # provider's OAuth app). Self-hosting? Set TREG_PUBLIC_URL to your deployment's URL.
    public_url: str = "https://treg.superdesign.dev"

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
    google_client_id: str = ""
    google_client_secret: str = ""
    # Overridable for tests; real Google by default.
    google_authorize_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"

    # Email one-time-code login (the third identity door). Dev mode RETURNS the code in the API
    # response, which is an unauthenticated account-takeover vector in prod — so it defaults OFF and
    # must be explicitly enabled (TREG_EMAIL_DEV_MODE=true) for local testing without a mail sender.
    email_dev_mode: bool = False

    @property
    def expose_dev_code(self) -> bool:
        """Dev login codes may be returned in the response ONLY on a local sqlite database — never on a
        real (Postgres) deploy. So even a stray TREG_EMAIL_DEV_MODE=true in production can't leak codes."""
        return self.email_dev_mode and "sqlite" in self.database_url

    # Transactional email via Resend (OTP sign-in codes + team invitations). Empty key = no real
    # send (dev mode still returns the code; prod without a key silently skips the send). From must
    # be a Resend-verified domain — treg.superdesign.dev is verified (DKIM + SPF).
    resend_api_key: str = ""
    email_from: str = "tools-registry <no-reply@treg.superdesign.dev>"


@lru_cache
def get_settings() -> Settings:
    return Settings()
