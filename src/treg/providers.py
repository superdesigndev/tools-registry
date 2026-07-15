"""Provider catalog + env scanner — the brains behind `treg upload` (env → auto-registered tools).

Phase 1 (this file): **detection only.** Read the variable NAMES from a dotenv file and classify each:
which third-party provider it belongs to, what **auth shape** the credential uses (so we register the
right kind of tool, not a broken one), which vars form a **pair** (OAuth client_id+secret), and which
are the app's **own internal** secrets that must never be exposed as a tool.

Nothing here touches the network or a value: `scan_env` splits each line at the first '=' and discards
the right-hand side. Registering (creating secrets/tools, the OAuth flow) is Phase 2.

The auth shapes map straight onto treg's binding model (see `convert.py` / `POST /tools`):
  - bearer          -> header  Authorization: "Bearer {secret}"
  - api_key_header  -> header  <header>: "{secret}"      (e.g. x-api-key)
  - basic           -> header  Authorization: "Basic ..."  (a PAIR: id + secret)
  - oauth2          -> NOT a binding; runs the /oauth/start connect flow (a PAIR: client_id + secret)
  - query           -> credential travels in a query param / URL, provider-specific
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Provider catalog ----------------------------------------------------------------------------
# `tokens` are matched against the underscore-delimited components of the (uppercased) var name, so an
# app prefix like TREG_RESEND_API_KEY still resolves via its "RESEND" component. Keep tokens
# distinctive (no generic KEY/TOKEN/SECRET — those are handled by the secret/internal heuristics).
# `auth` is the provider's DEFAULT shape; a per-variable form (CLIENT_ID/SECRET → oauth2) can override
# it. Served at GET /providers.json so the CLI can refresh centrally (bundled copy = offline fallback);
# bump CATALOG_VERSION whenever entries change so a cache can tell it's stale.
CATALOG_VERSION = 9
# `skills` (optional) matches a SKILL FOLDER name for file-credential skills that have no env var to
# key on (OAuth token files etc.) — see `match_skill`. Such providers carry `tokens: []` so the env
# scanner never mis-detects them as a simple bearer key (their real auth is OAuth + extra headers).
#
# `cli` (optional) is the provider's local-run behavior profile for `treg run` (docs/CLI-RUN-PLAN.md):
# {bin, install, inject[], deny[], errors[], noninteractive, warnings, verified}. `verified` carries the
# date of a real machine test — docs lie (vercel ships an env var it ignores), so documented ≠ verified.
# An `unsupported: true` cli block is first-class: it tells the analyzer WHY and what to do instead.
# The catalog can never ENABLE local runs — tool.cli.enabled (owner opt-in) controls that.
_ERR_AUTH = [{"pattern": r"(?i)\b401\b|unauthorized|invalid.{0,10}(api.)?key|authentication",
              "verdict": "credential_invalid", "message": "the org's credential is invalid or expired"}]
CATALOG: list[dict] = [
    {"provider": "Google Ads",  "tokens": [], "skills": ["google-ads", "googleads", "google-adwords"],
     "base_url": "https://googleads.googleapis.com",     "auth": {"shape": "bearer"}},
    {"provider": "Google Search Console", "tokens": [], "skills": ["gsc", "search-console", "google-search-console", "webmasters"],
     "base_url": "https://searchconsole.googleapis.com", "auth": {"shape": "bearer"}},
    {"provider": "Google Cloud", "tokens": [], "skills": ["gcloud", "google-cloud", "gcp"],
     "base_url": "https://cloudresourcemanager.googleapis.com", "auth": {"shape": "bearer"},
     "cli": {"bin": "gcloud", "login_cmd": "gcloud auth login", "install": "https://cloud.google.com/sdk/docs/install", "verified": "2026-07-07",
             "auth_mechanism": "config_file",  # normal use is `gcloud auth login` → local run reads its own config
             "detect": {"config_paths": ["~/.config/gcloud/credentials.db", "~/.config/gcloud/access_tokens.db"]},
             "inject": [{"via": "env", "name": "CLOUDSDK_AUTH_ACCESS_TOKEN", "secret_field": "token"}],
             "errors": _ERR_AUTH,
             "warnings": ["gcloud may also need a project: add a param secret + inject it as CLOUDSDK_CORE_PROJECT"]}},
    {"provider": "Azure", "tokens": [], "skills": ["az", "azure", "azure-cli"],
     "base_url": "https://management.azure.com", "auth": {"shape": "oauth2"},
     "cli": {"bin": "az", "unsupported": True, "auth_mechanism": "device",
             "reason": "az has no token-override env var (device/browser login only) — register an "
                       "Azure service principal as an HTTP tool instead"}},
    # tokens [] on purpose: SUPABASE_ANON_KEY / SERVICE_ROLE_KEY are per-project JWTs (project host,
    # not this management API) — an env-scan match would register them against the wrong base_url.
    {"provider": "Supabase",   "tokens": [], "skills": ["supabase", "supabase-cli"],
     "base_url": "https://api.supabase.com/v1", "auth": {"shape": "bearer"},
     "cli": {"bin": "supabase", "install": "brew install supabase/tap/supabase", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.supabase/access-token"]},
             "inject": [{"via": "env", "name": "SUPABASE_ACCESS_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "OpenAI",      "tokens": ["OPENAI"],              "base_url": "https://api.openai.com/v1",                       "auth": {"shape": "bearer"},
     "skills": ["openai", "openai-cli"],
     "cli": {"bin": "openai", "install": "pip install openai", "auth_mechanism": "env", "beta": True,
             "inject": [{"via": "env", "name": "OPENAI_API_KEY"}], "errors": _ERR_AUTH}},
    {"provider": "Anthropic",   "tokens": ["ANTHROPIC", "CLAUDE"], "base_url": "https://api.anthropic.com/v1",                    "auth": {"shape": "api_key_header", "header": "x-api-key"}},
    {"provider": "Google AI",   "tokens": ["GEMINI"],              "base_url": "https://generativelanguage.googleapis.com/v1beta","auth": {"shape": "api_key_header", "header": "x-goog-api-key"}},
    {"provider": "Mistral",     "tokens": ["MISTRAL"],             "base_url": "https://api.mistral.ai/v1",                       "auth": {"shape": "bearer"}},
    {"provider": "Cohere",      "tokens": ["COHERE"],              "base_url": "https://api.cohere.ai/v1",                        "auth": {"shape": "bearer"}},
    {"provider": "Groq",        "tokens": ["GROQ"],                "base_url": "https://api.groq.com/openai/v1",                  "auth": {"shape": "bearer"}},
    {"provider": "OpenRouter",  "tokens": ["OPENROUTER"],          "base_url": "https://openrouter.ai/api/v1",                    "auth": {"shape": "bearer"}},
    {"provider": "Perplexity",  "tokens": ["PERPLEXITY", "PPLX"],  "base_url": "https://api.perplexity.ai",                       "auth": {"shape": "bearer"}},
    {"provider": "HuggingFace", "tokens": ["HUGGINGFACE", "HUGGINGFACEHUB", "HF"], "base_url": "https://api-inference.huggingface.co", "auth": {"shape": "bearer"}},
    {"provider": "Replicate",   "tokens": ["REPLICATE"],           "base_url": "https://api.replicate.com/v1",                    "auth": {"shape": "bearer"}},
    {"provider": "ElevenLabs",  "tokens": ["ELEVENLABS", "ELEVEN"],"base_url": "https://api.elevenlabs.io/v1",                    "auth": {"shape": "api_key_header", "header": "xi-api-key"}},
    {"provider": "Deepgram",    "tokens": ["DEEPGRAM"],            "base_url": "https://api.deepgram.com/v1",                     "auth": {"shape": "api_key_header", "header": "Authorization", "format": "Token {secret}"}},
    {"provider": "Stripe",      "tokens": ["STRIPE"],              "base_url": "https://api.stripe.com/v1",                       "auth": {"shape": "bearer"}, "probe": "balance",
     "skills": ["stripe", "stripe-cli"],
     "cli": {"bin": "stripe", "install": "brew install stripe/stripe-cli/stripe", "verified": "2026-07-07",
             "auth_mechanism": "env", "detect": {"config_paths": ["~/.config/stripe/config.toml"]},
             "inject": [{"via": "env", "name": "STRIPE_API_KEY"}],
             "deny": [r"(^|\s)--live\b"],  # matches --live, --live=true, --live ... ; not --livemode. Creator can loosen via deny_defaults
             "errors": _ERR_AUTH}},
    {"provider": "Resend",      "tokens": ["RESEND"],              "base_url": "https://api.resend.com",                          "auth": {"shape": "bearer"}, "probe": "domains"},
    {"provider": "SendGrid",    "tokens": ["SENDGRID"],            "base_url": "https://api.sendgrid.com/v3",                     "auth": {"shape": "bearer"}},
    {"provider": "Postmark",    "tokens": ["POSTMARK"],            "base_url": "https://api.postmarkapp.com",                     "auth": {"shape": "api_key_header", "header": "X-Postmark-Server-Token"}},
    {"provider": "Twilio",      "tokens": ["TWILIO"],              "base_url": "https://api.twilio.com/2010-04-01",               "auth": {"shape": "basic"}},
    {"provider": "Slack",       "tokens": ["SLACK"],               "base_url": "https://slack.com/api",                           "auth": {"shape": "bearer"},
     "oauth": {"auth_uri": "https://slack.com/oauth/v2/authorize", "token_uri": "https://slack.com/api/oauth.v2.access", "scopes": ["users:read"]}},
    {"provider": "Discord",     "tokens": ["DISCORD"],             "base_url": "https://discord.com/api/v10",                     "auth": {"shape": "api_key_header", "header": "Authorization", "format": "Bot {secret}"},
     "oauth": {"auth_uri": "https://discord.com/oauth2/authorize", "token_uri": "https://discord.com/api/oauth2/token", "scopes": ["identify"]}},
    {"provider": "Telegram",    "tokens": ["TELEGRAM"],            "base_url": "https://api.telegram.org",                        "auth": {"shape": "query"}},
    {"provider": "GitHub",      "tokens": ["GITHUB", "GH"],        "base_url": "https://api.github.com",                          "auth": {"shape": "bearer"},
     "oauth": {"auth_uri": "https://github.com/login/oauth/authorize", "token_uri": "https://github.com/login/oauth/access_token", "scopes": ["read:user"]},
     "skills": ["gh", "github", "github-cli"],
     "cli": {"bin": "gh", "login_cmd": "gh auth login", "install": "brew install gh", "verified": "2026-07-07", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.config/gh/hosts.yml"]},
             # Deny features that would run member code as the runner, or print the injected token back:
             # `gh extension`/`alias` (arbitrary code), `gh auth token`/`--show-token` (echo the key).
             "deny": [r"(^|\s)(extension|alias)(\s|$)", r"(^|\s)auth\s+token(\s|$)", r"--show-token\b"],
             "inject": [{"via": "env", "name": "GH_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "GitLab",      "tokens": ["GITLAB"],              "base_url": "https://gitlab.com/api/v4",                        "auth": {"shape": "bearer"},
     "oauth": {"auth_uri": "https://gitlab.com/oauth/authorize", "token_uri": "https://gitlab.com/oauth/token", "scopes": ["read_api"]},
     "skills": ["glab", "gitlab", "gitlab-cli"],
     "cli": {"bin": "glab", "login_cmd": "glab auth login", "install": "brew install glab", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.config/glab-cli/config.yml"]},
             "inject": [{"via": "env", "name": "GITLAB_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Notion",      "tokens": ["NOTION"],              "base_url": "https://api.notion.com/v1",                       "auth": {"shape": "bearer"},
     "oauth": {"auth_uri": "https://api.notion.com/v1/oauth/authorize", "token_uri": "https://api.notion.com/v1/oauth/token", "scopes": []}},
    {"provider": "Linear",      "tokens": ["LINEAR"],              "base_url": "https://api.linear.app/graphql",                  "auth": {"shape": "api_key_header", "header": "Authorization", "format": "{secret}"},
     "oauth": {"auth_uri": "https://linear.app/oauth/authorize", "token_uri": "https://api.linear.app/oauth/token", "scopes": ["read"]}},
    {"provider": "Airtable",    "tokens": ["AIRTABLE"],            "base_url": "https://api.airtable.com/v0",                     "auth": {"shape": "bearer"}},
    {"provider": "Render",      "tokens": ["RENDER"],              "base_url": "https://api.render.com/v1",                       "auth": {"shape": "bearer"}, "probe": "services",
     "skills": ["render", "render-cli"],
     "cli": {"bin": "render", "install": "brew install render-oss/render/render", "auth_mechanism": "env", "beta": True,
             "inject": [{"via": "env", "name": "RENDER_API_KEY"}], "errors": _ERR_AUTH}},
    {"provider": "Vercel",      "tokens": ["VERCEL"],              "base_url": "https://api.vercel.com",                          "auth": {"shape": "bearer"}, "probe": "v2/user",
     "skills": ["vercel", "vercel-cli"],
     "cli": {"bin": "vercel", "install": "npm i -g vercel", "verified": "2026-07-07",
             # CLI 37.x ignores a VERCEL_TOKEN env var (tested) — inject via the --token flag instead.
             # argv is ps-visible on shared machines; noted, accepted (vercel offers no env path).
             # env_from names the env var to DISCOVER the value at import (the bridge reads it there).
             "auth_mechanism": "argv",
             "detect": {"config_paths": ["~/Library/Application Support/com.vercel.cli/auth.json",
                                         "~/.local/share/com.vercel.cli/auth.json"]},
             "inject": [{"via": "argv", "argv": ["--token", "{secret}"], "env_from": "VERCEL_TOKEN"}], "errors": _ERR_AUTH,
             "warnings": ["the token is passed as a --token flag (visible in `ps` on shared machines)"]}},
    {"provider": "Ahrefs",      "tokens": ["AHREFS"],              "base_url": "https://api.ahrefs.com/v3",                       "auth": {"shape": "bearer"}},
    {"provider": "Apify",       "tokens": ["APIFY"],               "base_url": "https://api.apify.com/v2",                        "auth": {"shape": "bearer"}, "probe": "users/me"},
    {"provider": "ScrapeCreators", "tokens": ["SCRAPECREATORS"],   "base_url": "https://api.scrapecreators.com",                  "auth": {"shape": "api_key_header", "header": "x-api-key"}},
    {"provider": "AgentMail",   "tokens": ["AGENTMAIL"],           "base_url": "https://api.agentmail.to/v0",                     "auth": {"shape": "bearer"}, "probe": "inboxes"},
    {"provider": "Cloudflare",  "tokens": ["CLOUDFLARE"],          "base_url": "https://api.cloudflare.com/client/v4",            "auth": {"shape": "bearer"},
     "skills": ["wrangler", "cloudflare", "cloudflare-workers"],
     "cli": {"bin": "wrangler", "install": "npm i -g wrangler", "auth_mechanism": "env",
             "inject": [{"via": "env", "name": "CLOUDFLARE_API_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Pinecone",    "tokens": ["PINECONE"],            "base_url": "https://api.pinecone.io",                         "auth": {"shape": "api_key_header", "header": "Api-Key"}},
    {"provider": "Stability",   "tokens": ["STABILITY"],           "base_url": "https://api.stability.ai/v1",                     "auth": {"shape": "bearer"}},
    # --- more AI / inference (bearer, OpenAI-compatible) ---
    {"provider": "Together AI", "tokens": ["TOGETHER"],            "base_url": "https://api.together.xyz/v1",                     "auth": {"shape": "bearer"}},
    {"provider": "Fireworks",   "tokens": ["FIREWORKS"],           "base_url": "https://api.fireworks.ai/inference/v1",           "auth": {"shape": "bearer"}},
    {"provider": "DeepSeek",    "tokens": ["DEEPSEEK"],            "base_url": "https://api.deepseek.com",                        "auth": {"shape": "bearer"}},
    {"provider": "xAI",         "tokens": ["XAI", "GROK"],         "base_url": "https://api.x.ai/v1",                             "auth": {"shape": "bearer"}},
    {"provider": "Voyage AI",   "tokens": ["VOYAGE"],              "base_url": "https://api.voyageai.com/v1",                     "auth": {"shape": "bearer"}},
    {"provider": "Jina AI",     "tokens": ["JINA"],                "base_url": "https://api.jina.ai/v1",                          "auth": {"shape": "bearer"}},
    {"provider": "AssemblyAI",  "tokens": ["ASSEMBLYAI"],          "base_url": "https://api.assemblyai.com/v2",                   "auth": {"shape": "api_key_header", "header": "Authorization", "format": "{secret}"}},
    {"provider": "Cartesia",    "tokens": ["CARTESIA"],            "base_url": "https://api.cartesia.ai",                         "auth": {"shape": "api_key_header", "header": "X-API-Key"}},
    # --- search / scraping ---
    {"provider": "Tavily",      "tokens": ["TAVILY"],              "base_url": "https://api.tavily.com",                          "auth": {"shape": "bearer"}},
    {"provider": "Firecrawl",   "tokens": ["FIRECRAWL"],           "base_url": "https://api.firecrawl.dev/v1",                    "auth": {"shape": "bearer"}},
    {"provider": "Exa",         "tokens": ["EXA"],                 "base_url": "https://api.exa.ai",                              "auth": {"shape": "api_key_header", "header": "x-api-key"}},
    {"provider": "Serper",      "tokens": ["SERPER"],              "base_url": "https://google.serper.dev",                       "auth": {"shape": "api_key_header", "header": "X-API-KEY"}},
    {"provider": "SerpAPI",     "tokens": ["SERPAPI"],             "base_url": "https://serpapi.com",                             "auth": {"shape": "query", "param": "api_key"}},
    {"provider": "Brave Search","tokens": ["BRAVE"],               "base_url": "https://api.search.brave.com/res/v1",             "auth": {"shape": "api_key_header", "header": "X-Subscription-Token"}},
    {"provider": "ScrapingBee", "tokens": ["SCRAPINGBEE"],         "base_url": "https://app.scrapingbee.com/api/v1",              "auth": {"shape": "query", "param": "api_key"}},
    {"provider": "NewsAPI",     "tokens": ["NEWSAPI"],             "base_url": "https://newsapi.org/v2",                          "auth": {"shape": "api_key_header", "header": "X-Api-Key"}},
    # --- dev / infra / cloud ---
    {"provider": "DigitalOcean","tokens": ["DIGITALOCEAN"],        "base_url": "https://api.digitalocean.com/v2",                 "auth": {"shape": "bearer"},
     "skills": ["doctl", "digitalocean"],
     "cli": {"bin": "doctl", "login_cmd": "doctl auth init", "install": "brew install doctl", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/Library/Application Support/doctl/config.yaml", "~/.config/doctl/config.yaml"]},
             "inject": [{"via": "env", "name": "DIGITALOCEAN_ACCESS_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Netlify",     "tokens": ["NETLIFY"],             "base_url": "https://api.netlify.com/api/v1",                  "auth": {"shape": "bearer"},
     "skills": ["netlify", "netlify-cli"],
     "cli": {"bin": "netlify", "install": "npm i -g netlify-cli", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.config/netlify/config.json", "~/Library/Preferences/netlify/config.json"]},
             "inject": [{"via": "env", "name": "NETLIFY_AUTH_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Heroku",      "tokens": ["HEROKU"],              "base_url": "https://api.heroku.com",                          "auth": {"shape": "bearer"},
     "skills": ["heroku", "heroku-cli"],
     "cli": {"bin": "heroku", "install": "brew install heroku/brew/heroku", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.netrc"]},
             "inject": [{"via": "env", "name": "HEROKU_API_KEY"}], "errors": _ERR_AUTH}},
    {"provider": "Fly.io",      "tokens": ["FLY"],                 "base_url": "https://api.machines.dev/v1",                     "auth": {"shape": "bearer"},
     "skills": ["fly", "flyctl", "fly-io"],
     "cli": {"bin": "flyctl", "login_cmd": "fly auth login", "install": "brew install flyctl", "verified": "2026-07-07", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.fly/config.yml"]},
             "deny": [r"(^|\s)auth\s+token(\s|$)"],  # `fly auth token` prints the injected key
             "inject": [{"via": "env", "name": "FLY_API_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Railway",     "tokens": ["RAILWAY"],             "base_url": "https://backboard.railway.app/graphql/v2",        "auth": {"shape": "bearer"},
     "skills": ["railway", "railway-cli"],
     "cli": {"bin": "railway", "install": "brew install railway", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.railway/config.json", "~/.config/railway/config.json"]},
             "inject": [{"via": "env", "name": "RAILWAY_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Neon",        "tokens": ["NEON"],                "base_url": "https://console.neon.tech/api/v2",                "auth": {"shape": "bearer"},
     "skills": ["neon", "neonctl"],
     "cli": {"bin": "neonctl", "install": "npm i -g neonctl", "auth_mechanism": "env", "beta": True,
             "inject": [{"via": "env", "name": "NEON_API_KEY"}], "errors": _ERR_AUTH}},
    {"provider": "PlanetScale", "tokens": ["PLANETSCALE"],         "base_url": "https://api.planetscale.com/v1",                  "auth": {"shape": "bearer"},
     "skills": ["pscale", "planetscale"],
     "cli": {"bin": "pscale", "login_cmd": "pscale auth login", "install": "brew install planetscale/tap/pscale", "auth_mechanism": "config_file", "beta": True,
             "detect": {"config_paths": ["~/.config/planetscale/access-token", "~/Library/Application Support/planetscale/access-token"]},
             "errors": _ERR_AUTH}},
    {"provider": "Turso",       "tokens": ["TURSO"],               "base_url": "https://api.turso.tech/v1",                       "auth": {"shape": "bearer"},
     "skills": ["turso", "turso-cli"],
     "cli": {"bin": "turso", "install": "brew install tursodatabase/tap/turso", "auth_mechanism": "env", "beta": True,
             "deny": [r"(^|\s)auth\s+token(\s|$)"],  # `turso auth token` prints the injected key
             "inject": [{"via": "env", "name": "TURSO_API_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Doppler",     "tokens": ["DOPPLER"],             "base_url": "https://api.doppler.com/v3",                      "auth": {"shape": "bearer"},
     "skills": ["doppler", "doppler-cli"],
     "cli": {"bin": "doppler", "install": "brew install dopplerhq/cli/doppler", "auth_mechanism": "env", "beta": True,
             # `doppler run -- <cmd>` and `secrets download` execute/emit with the token in env → key leak.
             "deny": [r"(^|\s)run(\s|$)", r"(^|\s)secrets\s+download(\s|$)"],
             "inject": [{"via": "env", "name": "DOPPLER_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Infisical",   "tokens": ["INFISICAL"],           "base_url": "https://app.infisical.com/api",                   "auth": {"shape": "bearer"},
     "skills": ["infisical", "infisical-cli"],
     "cli": {"bin": "infisical", "install": "brew install infisical/get-cli/infisical", "auth_mechanism": "env", "beta": True,
             # `infisical run -- <cmd>` and `export`/`secrets` emit or run with the token in env → key leak.
             "deny": [r"(^|\s)run(\s|$)", r"(^|\s)export(\s|$)"],
             "inject": [{"via": "env", "name": "INFISICAL_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Sentry",      "tokens": ["SENTRY"],              "base_url": "https://sentry.io/api/0",                         "auth": {"shape": "bearer"},
     "skills": ["sentry-cli", "sentry"],
     "cli": {"bin": "sentry-cli", "install": "brew install getsentry/tools/sentry-cli", "auth_mechanism": "env",
             "detect": {"config_paths": ["~/.sentryclirc"]},
             "inject": [{"via": "env", "name": "SENTRY_AUTH_TOKEN"}], "errors": _ERR_AUTH}},
    {"provider": "Datadog",     "tokens": ["DATADOG"],             "base_url": "https://api.datadoghq.com/api/v1",                "auth": {"shape": "api_key_header", "header": "DD-API-KEY"}},
    {"provider": "New Relic",   "tokens": ["NEWRELIC", "RELIC"],   "base_url": "https://api.newrelic.com/v2",                     "auth": {"shape": "api_key_header", "header": "X-Api-Key"}},
    {"provider": "PagerDuty",   "tokens": ["PAGERDUTY"],           "base_url": "https://api.pagerduty.com",                       "auth": {"shape": "api_key_header", "header": "Authorization", "format": "Token token={secret}"}},
    {"provider": "Honeycomb",   "tokens": ["HONEYCOMB"],           "base_url": "https://api.honeycomb.io/1",                      "auth": {"shape": "api_key_header", "header": "X-Honeycomb-Team"}},
    {"provider": "Bunny",       "tokens": ["BUNNY"],               "base_url": "https://api.bunny.net",                           "auth": {"shape": "api_key_header", "header": "AccessKey"}},
    # --- payments / commerce ---
    {"provider": "Square",      "tokens": ["SQUARE"],              "base_url": "https://connect.squareup.com/v2",                 "auth": {"shape": "bearer"}},
    {"provider": "Paddle",      "tokens": ["PADDLE"],              "base_url": "https://api.paddle.com",                          "auth": {"shape": "bearer"}},
    {"provider": "LemonSqueezy","tokens": ["LEMONSQUEEZY"],        "base_url": "https://api.lemonsqueezy.com/v1",                 "auth": {"shape": "bearer"}},
    {"provider": "Coinbase",    "tokens": ["COINBASE"],            "base_url": "https://api.coinbase.com/v2",                     "auth": {"shape": "bearer"}},
    # --- comms / messaging ---
    {"provider": "Intercom",    "tokens": ["INTERCOM"],            "base_url": "https://api.intercom.io",                         "auth": {"shape": "bearer"}, "probe": "me"},
    {"provider": "Front",       "tokens": ["FRONT"],               "base_url": "https://api2.frontapp.com",                       "auth": {"shape": "bearer"}},
    {"provider": "Telnyx",      "tokens": ["TELNYX"],              "base_url": "https://api.telnyx.com/v2",                       "auth": {"shape": "bearer"}},
    {"provider": "MessageBird", "tokens": ["MESSAGEBIRD"],         "base_url": "https://rest.messagebird.com",                    "auth": {"shape": "api_key_header", "header": "Authorization", "format": "AccessKey {secret}"}},
    {"provider": "Loops",       "tokens": ["LOOPS"],               "base_url": "https://app.loops.so/api/v1",                     "auth": {"shape": "bearer"}},
    {"provider": "Nylas",       "tokens": ["NYLAS"],               "base_url": "https://api.us.nylas.com/v3",                     "auth": {"shape": "bearer"}},
    # --- productivity / CRM / product ---
    {"provider": "HubSpot",     "tokens": ["HUBSPOT"],             "base_url": "https://api.hubapi.com",                          "auth": {"shape": "bearer"}},
    {"provider": "Asana",       "tokens": ["ASANA"],               "base_url": "https://app.asana.com/api/1.0",                   "auth": {"shape": "bearer"}},
    {"provider": "ClickUp",     "tokens": ["CLICKUP"],             "base_url": "https://api.clickup.com/api/v2",                  "auth": {"shape": "api_key_header", "header": "Authorization", "format": "{secret}"}},
    {"provider": "Monday",      "tokens": ["MONDAY"],              "base_url": "https://api.monday.com/v2",                       "auth": {"shape": "api_key_header", "header": "Authorization", "format": "{secret}"}},
    {"provider": "Calendly",    "tokens": ["CALENDLY"],            "base_url": "https://api.calendly.com",                        "auth": {"shape": "bearer"}},
    {"provider": "Cal.com",     "tokens": ["CALCOM"],              "base_url": "https://api.cal.com/v1",                          "auth": {"shape": "query", "param": "apiKey"}},
    {"provider": "Typeform",    "tokens": ["TYPEFORM"],            "base_url": "https://api.typeform.com",                        "auth": {"shape": "bearer"}},
    {"provider": "Zoom",        "tokens": ["ZOOM"],                "base_url": "https://api.zoom.us/v2",                          "auth": {"shape": "bearer"}},
    {"provider": "Pipedrive",   "tokens": ["PIPEDRIVE"],           "base_url": "https://api.pipedrive.com/v1",                    "auth": {"shape": "query", "param": "api_token"}},
    {"provider": "PostHog",     "tokens": ["POSTHOG"],             "base_url": "https://app.posthog.com",                         "auth": {"shape": "bearer"}},
    # --- media / content ---
    {"provider": "Unsplash",    "tokens": ["UNSPLASH"],            "base_url": "https://api.unsplash.com",                        "auth": {"shape": "api_key_header", "header": "Authorization", "format": "Client-ID {secret}"}},
    {"provider": "Giphy",       "tokens": ["GIPHY"],               "base_url": "https://api.giphy.com/v1",                        "auth": {"shape": "query", "param": "api_key"}},
    {"provider": "OpenWeather", "tokens": ["OPENWEATHER", "OWM"],  "base_url": "https://api.openweathermap.org/data/2.5",         "auth": {"shape": "query", "param": "appid"}},
]

# Per-variable credential FORMS that override a provider's default auth, and combine two vars into one
# credential. Each: the two name-components that pair up, and the resulting auth shape.
PAIR_FORMS = [
    {"form": "oauth2", "parts": ("CLIENT_ID", "CLIENT_SECRET")},   # GitHub/Google OAuth apps
    {"form": "basic",  "parts": ("ACCOUNT_SID", "AUTH_TOKEN")},    # Twilio-style basic auth
]

# The app's OWN secrets — never a callable upstream. Matched as compound substrings of the (uppercased)
# name so a prefix doesn't hide them. Checked only AFTER provider match, so e.g. OPENAI_SECRET_KEY still
# resolves to OpenAI (a real provider key), while a bare SECRET_KEY is correctly flagged internal.
APP_INTERNAL = [
    "SECRET_KEY", "SESSION_SECRET", "JWT_SECRET", "ENCRYPTION_KEY", "FERNET", "SIGNING_KEY",
    "COOKIE_SECRET", "CSRF", "APP_SECRET", "APP_KEY", "DATABASE_URL", "DB_URL", "DB_PASSWORD",
    "ADMIN_TOKEN", "ADMIN_PASSWORD", "SALT", "WEBHOOK_SECRET",
]

# Generic components that mark a var as a credential (worth an LLM lookup even w/o a provider match).
SECRET_HINTS = {"KEY", "TOKEN", "SECRET", "PASSWORD", "PWD", "APIKEY", "CREDENTIAL", "CREDENTIALS",
                "AUTH", "ACCESS", "PRIVATE", "BEARER"}

# Components that mark a var as plain CONFIG, not a credential. A provider-token match on one of these
# (with no secret hint) — e.g. POSTHOG_HOST, OPENAI_MODEL, SENTRY_DSN, SUPABASE_URL — is config, not a tool.
CONFIG_HINTS = {"URL", "URI", "HOST", "PORT", "FROM", "MODE", "ENV", "REGION", "DEBUG", "LEVEL",
                "PATH", "DIR", "NAME", "ID", "VERSION", "PUBLIC", "TIMEOUT", "ENABLED", "DISABLE",
                "PROJECT", "DOMAIN", "ENDPOINT", "PREFIX", "BUCKET", "ORG", "MODEL", "BASE",
                "ORGANIZATION", "HOME", "DSN"}

# Compound names that are ALWAYS the app's own secret, even when a provider token also matches —
# STRIPE_WEBHOOK_SECRET is a signing secret, not a callable Stripe key (unlike OPENAI_SECRET_KEY).
ALWAYS_INTERNAL = ("WEBHOOK_SECRET", "SIGNING_KEY", "SIGNING_SECRET", "JWT_SECRET", "ENCRYPTION_KEY",
                   "COOKIE_SECRET", "SESSION_SECRET")


@dataclass
class Detection:
    """One classified credential (or credential pair) found in the env."""
    kind: str                          # matched | oauth_pair | basic_pair | app_internal | unknown_secret | config
    vars: list[str] = field(default_factory=list)
    provider: str | None = None
    base_url: str | None = None
    auth: dict | None = None           # resolved auth shape (see module docstring)
    probe: str | None = None           # a cheap GET path (relative to base_url) that validates the key
    note: str | None = None


# --- name extraction (VALUES are split off and discarded) ----------------------------------------
def var_names(env_path: str) -> list[str]:
    names: list[str] = []
    with open(env_path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip()          # index 1 (the value) is never kept
            if name.lower().startswith("export "):
                name = name[len("export "):].strip()
            if name:
                names.append(name)
    return names


def env_values(env_path: str, names: list[str]) -> dict[str, str]:
    """Read the VALUES for specific variables — used by `treg upload` at registration time only
    (never by detection). Returns {name: value} for the requested names, others ignored."""
    want, out = set(names), {}
    with open(env_path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.lower().startswith("export "):
                key = key[len("export "):].strip()
            if key in want:
                v = val.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]          # strip ONE balanced surrounding quote pair, not chained
                out[key] = v
    return out


def _match_provider(comps: set[str], catalog: list[dict]) -> dict | None:
    best, best_len = None, -1
    for p in catalog:
        for tok in p["tokens"]:
            if tok in comps and len(tok) > best_len:
                best, best_len = p, len(tok)          # longest token wins (HUGGINGFACE over HF)
    return best


def match_skill(name: str, catalog: list[dict] | None = None) -> dict | None:
    """Match a SKILL FOLDER name to a catalog provider — for file-credential skills (OAuth token files
    etc.) that ship no env var to key on. Matches an explicit `skills` alias or the normalized provider
    name (case/punctuation-insensitive). Returns the catalog entry (its curated `base_url` + `auth`) or
    None. Lets the generator give e.g. google-ads / gsc the RIGHT upstream host, not a heuristic guess."""
    catalog = catalog if catalog is not None else CATALOG
    norm = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")  # noqa: E731
    n = norm(name)
    if not n:
        return None
    for p in catalog:
        if n in [norm(a) for a in p.get("skills", [])] or norm(p["provider"]) == n:
            return p
    return None


# --- CLI auto-import (Phase 2): classify an installed CLI into an import decision -----------------
def cli_env_var(cli: dict) -> str | None:
    """The env var a CLI reads its credential from (for the 'needs a key' hint) — the first `env` inject
    `name`, or an `argv` inject's `env_from`. None if the profile injects nothing (config_file/device)."""
    for e in cli.get("inject") or []:
        if e.get("via", "env") == "env" and e.get("name"):
            return e["name"]
        if e.get("via") == "argv" and e.get("env_from"):
            return e["env_from"]
    return None


def classify_cli(entry: dict, *, installed: bool, secret_present: bool, logged_in: bool) -> dict:
    """Decide what auto-import should do with one catalog CLI, given machine facts the caller gathered
    (is the `bin` installed? is its credential env var set? is a login config file present?). PURE — no
    filesystem/network here, so it's fully unit-testable. Returns a decision dict:
      {status, tier?, env?, action?, reason?} where status ∈
      ready | needs_key | needs_login | unsupported | not_installed, and tier ∈ server | local.
    See docs/CLI-AUTOIMPORT-PLAN.md §5."""
    cli = entry.get("cli") or {}
    mech = cli.get("auth_mechanism", "env")
    if cli.get("unsupported") or mech == "device":
        return {"status": "unsupported", "reason": cli.get("reason") or "not automatable"}
    if not installed:
        return {"status": "not_installed", "action": cli.get("install")}
    login = cli.get("login_cmd") or f"{cli.get('bin')} login"  # exact login command (varies: `gh auth login`, …)
    if mech in ("env", "argv"):
        if secret_present:
            return {"status": "ready", "tier": "server"}   # key available → inject server-side
        if logged_in:
            return {"status": "ready", "tier": "local"}     # no key, but the CLI is logged in → run locally
        # missing the key; if the CLI also supports a login (has a detect path), offer that as the alternative
        d = {"status": "needs_key", "env": cli_env_var(cli)}
        if cli.get("detect"):
            d["login"] = login
        return d
    # config_file: the credential lives in the CLI's own config → local only
    if logged_in:
        return {"status": "ready", "tier": "local"}
    return {"status": "needs_login", "action": f"run `{login}`"}


def _pair_form(upper: str) -> tuple[str, str] | None:
    """Return (form, part) if the name is half of a known credential pair, else None."""
    for pf in PAIR_FORMS:
        for part in pf["parts"]:
            if part in upper:
                return pf["form"], part


def _pair_prefix(name: str, part: str) -> str:
    """The var name with the pair-part component removed — halves that share this prefix belong to the
    SAME app (so GITHUB_A_CLIENT_ID + GITHUB_A_CLIENT_SECRET pair up, but GITHUB_B_* stays separate)."""
    return name.upper().replace(part, "").strip("_")


def _is_internal(upper: str) -> bool:
    return any(pat in upper for pat in APP_INTERNAL)


def scan_env(env_path: str, catalog: list[dict] | None = None) -> list[Detection]:
    """Classify every variable in the dotenv file. Names only — no values are read. `catalog` defaults
    to the bundled CATALOG; the CLI can pass a server-refreshed one (GET /providers.json)."""
    catalog = catalog if catalog is not None else CATALOG
    raw = []                       # (name, provider|None, pairform|None)
    for name in var_names(env_path):
        upper = name.upper()
        comps = set(upper.split("_"))
        provider = _match_provider(comps, catalog)
        pf = _pair_form(upper)
        # A Basic pair (ACCOUNT_SID/AUTH_TOKEN) only applies to a genuinely basic-auth provider (Twilio).
        # Otherwise *_AUTH_TOKEN (e.g. SENTRY_AUTH_TOKEN) is just a Bearer token, not half a Basic pair.
        if pf and pf[0] == "basic" and (provider or {}).get("auth", {}).get("shape") != "basic":
            pf = None
        raw.append((name, provider, pf))

    # First, fold paired credentials (same provider + same form) into one detection.
    detections: list[Detection] = []
    used = set()
    for i, (name, provider, pf) in enumerate(raw):
        if i in used or pf is None or provider is None:
            continue
        form, part = pf
        prefix = _pair_prefix(name, part)
        mates = [i] + [j for j, (n2, p2, pf2) in enumerate(raw)
                       if j != i and j not in used and p2 and p2["provider"] == provider["provider"]
                       and pf2 and pf2[0] == form and pf2[1] != part          # the OTHER half
                       and _pair_prefix(n2, pf2[1]) == prefix]                 # same app (shared prefix)
        mates = mates[:2]                                                     # a pair is exactly two
        if len(mates) >= 2:
            for j in mates:
                used.add(j)
            kind = "oauth_pair" if form == "oauth2" else "basic_pair"
            auth = {"shape": form}
            if form == "oauth2" and provider.get("oauth"):
                auth.update(provider["oauth"])       # carry auth_uri/token_uri/scopes for the connect flow
            detections.append(Detection(
                kind=kind, vars=[raw[j][0] for j in mates], provider=provider["provider"],
                base_url=provider["base_url"], auth=auth,
                note="OAuth app pair → connect flow, not a Bearer key" if form == "oauth2" else "id + secret → Basic auth",
            ))

    # Then classify the remaining singletons.
    for i, (name, provider, pf) in enumerate(raw):
        if i in used:
            continue
        upper = name.upper()
        comps = set(upper.split("_"))
        if provider is not None:
            # A lone half of a pair (e.g. only CLIENT_ID present) — still an oauth detection, incomplete.
            if pf is not None and pf[0] in ("oauth2", "basic"):
                detections.append(Detection(
                    kind="oauth_pair" if pf[0] == "oauth2" else "basic_pair", vars=[name],
                    provider=provider["provider"], base_url=provider["base_url"], auth={"shape": pf[0]},
                    note=f"incomplete pair — only the {pf[1]} half found"))
            elif any(pat in upper for pat in ALWAYS_INTERNAL):
                # STRIPE_WEBHOOK_SECRET / *_SIGNING_KEY — a signing secret, not a callable provider key.
                detections.append(Detection(kind="app_internal", vars=[name],
                                            note=f"{provider['provider']} signing/app secret — not a callable key"))
            elif (comps & CONFIG_HINTS) and not (comps & SECRET_HINTS):
                # A provider token on a clearly-CONFIG var (HOST/URL/ID/REGION, no secret hint) is not a
                # credential — e.g. POSTHOG_HOST, POSTHOG_PROJECT_ID, SUPABASE_URL. Don't register it.
                detections.append(Detection(kind="config", vars=[name],
                                            note=f"{provider['provider']} config (not a credential)"))
            else:
                detections.append(Detection(
                    kind="matched", vars=[name], provider=provider["provider"],
                    base_url=provider["base_url"], auth=dict(provider["auth"]),
                    probe=provider.get("probe")))
        elif _is_internal(upper):
            detections.append(Detection(kind="app_internal", vars=[name], note="app's own secret — never a tool"))
        elif comps & SECRET_HINTS:
            detections.append(Detection(kind="unknown_secret", vars=[name], note="looks like a credential; no provider match"))
        else:
            detections.append(Detection(kind="config", vars=[name]))
    return detections


# --- Phase 2: turn a detection into concrete registration payloads -------------------------------
def _slug(provider: str) -> str:
    return provider.lower().replace(" ", "-")


def build_binding(auth: dict) -> dict | None:
    """Translate an auth shape into a treg tool binding (see convert.py / POST /tools). Returns None
    for `oauth2` (handled by the connect flow, not a static binding). `basic` yields a header binding
    over a PRE-COMBINED base64 secret (the CLI base64s id:secret at register time — see cmd_import)."""
    shape = auth.get("shape")
    if shape == "bearer":
        return {"injector": "env", "location": "header", "name": "Authorization", "format": "Bearer {secret}"}
    if shape == "api_key_header":
        return {"injector": "env", "location": "header",
                "name": auth.get("header", "Authorization"), "format": auth.get("format", "{secret}")}
    if shape == "query":
        param = auth.get("param")
        if not param:            # no param name (e.g. Telegram's path-embedded token) → can't auto-bind
            return None
        return {"injector": "env", "location": "query", "name": param, "format": "{secret}"}
    if shape == "basic":
        return {"injector": "env", "location": "header", "name": "Authorization", "format": "Basic {secret}"}
    return None


def oauth_parts(names: list[str]) -> tuple[str | None, str | None]:
    """From an oauth pair's var names, return (client_id_var, client_secret_var)."""
    cid = next((v for v in names if "CLIENT_ID" in v.upper()), None)
    csec = next((v for v in names if "CLIENT_SECRET" in v.upper()), None)
    return cid, csec


def oauth_ready(det: Detection) -> bool:
    """True for a COMPLETE oauth pair whose provider has connect-flow endpoints in the catalog."""
    cid, csec = oauth_parts(det.vars)
    return bool(cid and csec and det.auth and det.auth.get("auth_uri") and det.auth.get("token_uri"))


def basic_parts(names: list[str]) -> tuple[str | None, str | None]:
    """From a basic pair's var names, return (username_var, password_var) — e.g. Twilio
    (ACCOUNT_SID, AUTH_TOKEN). The username half carries SID/ACCOUNT/USER/ID; the other is the secret."""
    user = next((v for v in names if any(t in v.upper() for t in ("SID", "ACCOUNT", "USER"))), None)
    pw = next((v for v in names if v != user
               and any(t in v.upper() for t in ("TOKEN", "SECRET", "PASSWORD", "KEY"))), None)
    return user, pw


@dataclass
class Action:
    """A planned registration for one detection: what secret + tool (or why we can't auto-do it)."""
    detection: Detection
    supported: bool
    secret_name: str | None = None
    tool_name: str | None = None
    base_url: str | None = None
    binding: dict | None = None     # binding template; secret_id is filled in at register time
    combine: tuple | None = None    # (username_var, password_var) for basic pairs → base64 at register
    health: dict | None = None      # {path, expect_status} probe from the catalog, so the tool self-validates
    reason: str | None = None       # why unsupported


def plan_actions(detections: list[Detection]) -> list[Action]:
    """Build a registration Action for each OFFERABLE detection (matched / pair / unknown_secret).
    app_internal + config are never offered, so they're skipped here."""
    actions: list[Action] = []
    for d in detections:
        if d.kind == "matched":
            binding = build_binding(d.auth or {})
            actions.append(Action(
                detection=d, supported=binding is not None, secret_name=d.vars[0],
                tool_name=_slug(d.provider or d.vars[0]), base_url=d.base_url, binding=binding,
                health={"path": d.probe, "expect_status": 200} if d.probe else None,
                reason=None if binding else f"auth shape {d.auth.get('shape') if d.auth else '?'} not auto-registered yet"))
        elif d.kind == "oauth_pair":
            actions.append(Action(detection=d, supported=False, base_url=d.base_url,
                                  tool_name=_slug(d.provider or ""), reason="OAuth — use `treg oauth connect` (Phase 2.5)"))
        elif d.kind == "basic_pair":
            user_var, pass_var = basic_parts(d.vars)
            if user_var and pass_var:
                actions.append(Action(
                    detection=d, supported=True, base_url=d.base_url, tool_name=_slug(d.provider or ""),
                    secret_name=f"{_slug(d.provider or '').replace('-', '_').upper()}_BASIC",
                    binding=build_binding({"shape": "basic"}), combine=(user_var, pass_var)))
            else:
                actions.append(Action(detection=d, supported=False, base_url=d.base_url,
                                      tool_name=_slug(d.provider or ""), reason="incomplete Basic pair"))
        elif d.kind == "unknown_secret":
            actions.append(Action(detection=d, supported=False, secret_name=d.vars[0],
                                  reason="no provider match — base_url needed (LLM/manual, Phase 4)"))
    # Disambiguate tool-name collisions among registerable actions — two matched vars for one provider
    # (e.g. GITHUB_TOKEN + GH_TOKEN → both "github") would clash at register (unique tool name per org).
    used: set[str] = set()
    for a in actions:
        if not a.supported or not a.tool_name:
            continue
        base, n = a.tool_name, 2
        while a.tool_name in used:
            a.tool_name = f"{base}-{n}"; n += 1
        used.add(a.tool_name)
    return actions


# --- Phase 4: LLM fallback for unknown_secret vars (opt-in via `treg upload --llm`) ---------------
LLM_SYSTEM = (
    "You identify which third-party API an environment variable authenticates, from its NAME only. "
    "For each name that is clearly a real, public API provider's credential, return the provider, its "
    "REST API base_url (https, the API root — no trailing resource path), and the auth shape. SKIP "
    "anything that is an app's own secret (SECRET_KEY, SESSION_SECRET, DATABASE_URL…) or that you can't "
    "confidently map to a specific provider."
)


def llm_prompt(names: list[str]) -> tuple[str, str]:
    """Build (system, user) messages asking an LLM to resolve unknown var names to providers."""
    ask = (
        'Map these env var names to API providers. Return ONLY JSON: {"resolved":[{"var":"NAME",'
        '"provider":"Name","base_url":"https://...","auth":{"shape":"bearer|api_key_header|query",'
        '"header":"X-Api-Key","param":"api_key"}}]}. Include "header" only for api_key_header, "param" '
        "only for query. Omit any name you are unsure about.\nNames: " + ", ".join(names)
    )
    return LLM_SYSTEM, ask


def llm_parse(text: str) -> list[dict]:
    """Parse the LLM's JSON reply into resolution dicts, tolerating prose around the JSON. Keeps only
    entries with a var + base_url + a known auth shape."""
    import json
    try:
        data = json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            return []
        try:
            data = json.loads(text[i:j + 1])
        except Exception:
            return []
    if not isinstance(data, dict):   # model returned a bare array/scalar — don't crash on .get
        return []
    out, seen = [], set()
    for e in (data.get("resolved") or []):
        auth = e.get("auth") or {}
        var = e.get("var")
        if not (var and e.get("base_url") and auth.get("shape") in ("bearer", "api_key_header", "query")):
            continue
        if var in seen or not _safe_base_url(e["base_url"]):   # dedupe; reject non-https / private / malformed
            continue
        seen.add(var)
        out.append(e)
    return out


def _safe_base_url(url: str) -> bool:
    """A base_url safe to register as a proxied upstream: https + a real public host (not loopback,
    private, or link-local — an LLM hallucination or SSRF vector otherwise)."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or "." not in host:
        return False
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        return False
    if host.startswith(("127.", "10.", "192.168.", "169.254.", "0.")) or host == "::1":
        return False
    if host.startswith("172."):                       # 172.16.0.0–172.31.255.255 private range
        try:
            if 16 <= int(host.split(".")[1]) <= 31:
                return False
        except (ValueError, IndexError):
            pass
    return True


# --- dev/test entrypoint (Phase 1): `python -m treg.providers [--env-dir DIR]` --------------------
def _report(env_dir: str) -> None:
    import os
    path = os.path.join(env_dir, ".env")
    dets = scan_env(path)
    order = ["matched", "oauth_pair", "basic_pair", "unknown_secret", "app_internal", "config"]
    labels = {
        "matched": "● MATCHED — register as a tool",
        "oauth_pair": "◆ OAUTH PAIR — connect flow (not a Bearer key)",
        "basic_pair": "◆ BASIC PAIR — id + secret",
        "unknown_secret": "○ UNKNOWN credential — LLM lookup / manual base_url",
        "app_internal": "· APP-INTERNAL — excluded (never a tool)",
        "config": "· config — skipped",
    }
    print(f"Scanned {path}\n")
    for k in order:
        group = [d for d in dets if d.kind == k]
        if not group:
            continue
        print(labels[k])
        for d in group:
            vs = " + ".join(d.vars)
            if d.provider:
                auth = d.auth.get("shape") if d.auth else "?"
                hdr = f" ({d.auth.get('header')})" if d.auth and d.auth.get("header") else ""
                print(f"    {vs}")
                print(f"        → {d.provider}: {d.base_url}   [{auth}{hdr}]" + (f"  — {d.note}" if d.note else ""))
            else:
                print(f"    {vs}" + (f"   — {d.note}" if d.note else ""))
        print()


def _plan_report(env_dir: str) -> None:
    import os
    path = os.path.join(env_dir, ".env")
    actions = plan_actions(scan_env(path))
    ok = [a for a in actions if a.supported]
    skip = [a for a in actions if not a.supported]
    print(f"Registration plan for {path}\n")
    print(f"WILL REGISTER ({len(ok)}) — creates a secret (kind=env) + a tool:")
    for a in ok:
        b = a.binding or {}
        inj = f'{b.get("name")}: {b.get("format")}'
        print(f"    tool '{a.tool_name}'  →  {a.base_url}")
        print(f"        secret  {a.secret_name}  (value read from env at run time, never shown)")
        print(f"        binding {inj}")
    print(f"\nNEEDS A DIFFERENT PATH ({len(skip)}):")
    for a in skip:
        vs = " + ".join(a.detection.vars)
        print(f"    {vs}  —  {a.reason}")


if __name__ == "__main__":
    import argparse, os
    ap = argparse.ArgumentParser(description="treg provider env-scan (Phase 1 detect + Phase 2 plan)")
    ap.add_argument("--env-dir", default=os.getcwd())
    ap.add_argument("--plan", action="store_true", help="show the registration plan (dry-run, no network)")
    args = ap.parse_args()
    (_plan_report if args.plan else _report)(args.env_dir)
