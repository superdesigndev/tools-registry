# OSS-prep — review notes

This directory (`tools-registry-oss`) is a **staging copy** for the public release, assembled from the
private repo. **Nothing in the private repo or on GitHub was changed.** Review this tree; when you're
happy, we create the new private `tools-registry` repo from it. This notes file does NOT ship.

---

## 1. What was EXCLUDED (stays in the private archive only)

- `CLAUDE.md` (internal agent instructions), `JOURNAL.md` (dev narrative)
- `docs/HANDOFF-*` (10 files — incl. the one with the 5 QA tokens)
- `docs/*-PLAN.md` (7 — incl. `SECURITY-FIXES-PLAN.md`, which maps past vulnerabilities → keep private)
- `docs/BUGS.md`, `docs/CLI-RUN-MACHINE-TEST.md`, `docs/dash-tour/`, `docs/dashboard-mockup.html`,
  `docs/style-board.html`
- `meetings/` (Jason meeting notes)
- `.claude/mode`, `.claude/scheduled_tasks.lock`

## 2. What was SANITIZED / removed (secret hygiene)

- **Removed leaked backups that rsync pulled in** — every `*.db`, `*.db.*.bak`, and `.env.bak*`
  (these held REAL data + secrets). Verified gone.
- **`src/treg/sandbox.py`** demo values → obvious placeholders (`sk_live_DEMO0000PLACEHOLDER`,
  `phx_DEMO0000PLACEHOLDER`).
- **Secret scan of this tree is clean** — the only remaining pattern hits are intentional fakes
  (`sk_live_ABCDEFGHIJKLMNOP1234` redaction fixture, `sk_test_123`) — allowlisted in `.gitleaks.toml`.

## 3. Scaffolding ADDED (all DRAFT — you plan to author the real versions)

| File | Status |
|---|---|
| `.gitleaks.toml` | ready — allowlists the fake fixtures |
| `.github/workflows/ci.yml` | ready — runs `pytest` + gitleaks on every PR |
| `.github/dependabot.yml` | ready — weekly pip + actions updates |
| `SECURITY.md` | DRAFT — real content (disclosure + security model + known limitations); **set the contact email** |
| `CONTRIBUTING.md` | DRAFT skeleton — yours to expand |
| `AGENTS.md` | DRAFT skeleton — the public equivalent of the private `CLAUDE.md`, for AI collaborators |

---

## 4. STILL TO DECIDE / DO before this goes public

### A. Functional — the app currently DEFAULTS to your infrastructure
These make the published app point at your boxes out of the box. Recommend genericizing:
- `src/treg/config.py:70` — `public_url` default = `https://treg.ngrok.app` → suggest `http://localhost:8000`
- `src/treg/config.py:107` — `email_from` default = `no-reply@treg.superdesign.dev` → suggest a placeholder
- `src/treg/cli.py:2306` — base-url fallback = `https://treg.superdesign.dev` → suggest `http://localhost:8000`
- `render.yaml` — points at your prod URL + DB; turn into a neutral example (or move to `docs/deploy/`)

### A2. Vendor the external front-end resources (make the app self-contained)
The dashboard/landing are Vue with **no build step** — good — but the served HTML fetches several
resources from third-party CDNs at runtime. Full list (as of the re-sync from private `main`):
- `src/treg/web/index.html` — Vue (`unpkg.com/vue@3`), Google Fonts (`fonts.googleapis.com` +
  `fonts.gstatic.com`)
- `src/treg/web/landing.html` — Google Fonts, and brand icons from `cdn.simpleicons.org`
- `tutorial.html` — check for Vue/fonts too

For a self-contained public app (no internet needed to boot, no CDN-availability / supply-chain trust):
1. Vendor `vue.global.prod.js` into `src/treg/web/` and point the script tag at `/vue.global.prod.js`.
2. Self-host the few fonts (download the woff2 files + a local `@font-face` CSS) instead of Google Fonts.
3. Replace `cdn.simpleicons.org` icons with inline SVGs or a small vendored icon set.
This is a "nice to have" for polish/robustness, not a blocker — but worth doing before a public launch.

### B. Privacy — real person / company in shipped assets
- `src/treg/web/index.html` — `jason@superdesign.dev` (a real collaborator) → a neutral example address
- `docs/ONBOARDING.md` — `you@kidocode.com` → `you@example.com`

### C. Cosmetic — example URLs / personas (a batch find-replace, your call on scope)
- `treg.superdesign.dev` / `treg.ngrok.app` appear as **example registry URLs** across README, USAGE,
  the tutorials, and `docs/context/*`. Not secrets — but for a clean public repo consider a placeholder
  like `https://treg.example.com`. The `ngrok-skip-browser-warning` headers in `cli.py` are ngrok-specific
  (harmless).
- Persona emails in tutorials (`tom@`/`bob@`/`alice@superdesign.dev`, `sam@`) read as fine illustrative
  examples — keep or genericize, your preference.
- `docs/context/ops/deploy.md` documents your exact Render/ngrok/Mac-Studio setup — genericize or keep as
  a real-world example.

### D. Files to add
- `LICENSE` (you chose "decide later")
- `CODE_OF_CONDUCT.md` (e.g. Contributor Covenant)
- Public-facing polish of `README.md` (drop dev-box specifics; add badges, quickstart, license)
- Optionally `.github/ISSUE_TEMPLATE/` + a PR template

---

## 5. The GitHub choreography (when the tree is approved) — protects prod

Production auto-deploys from `superdesigndev/tools-registry` → `main`. Safe order, with a prod check
after each step:
1. Rename `tools-registry` → `tools-registry-private` on GitHub.
2. Update this machine's git remote to the `-private` URL.
3. **Verify Render still deploys from `-private`** (Render API) — before continuing.
4. Create the new empty **private** `tools-registry`.
5. **Re-verify Render is still pinned to `-private`** (the critical collision check).
6. Push this approved tree as the new repo's first commit; keep it private until ready; then flip public
   and enable private vulnerability reporting + secret-scanning push-protection + branch protection.
