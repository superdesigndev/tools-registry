# tools-registry - hands-on tutorial

The whole registry, end to end. Every step shows the **exact command**, the **expected output**, and
**what to notice** - so it reads standalone. Copy each command into your terminal and follow along.

There are two companion versions of this same walkthrough, generated from one source
(`src/treg/web/tutorial.js`):

- **In the dashboard** → sign in at `https://treg.ngrok.app/` and open **Help → Tutorial**.
- **Standalone** → `https://treg.ngrok.app/tutorial`.

### Two focused, deep-dive tutorials

Two features have their own detailed, step-by-step tutorials (exact commands, real output, and how each
was tested):

- **Import & shell** → `https://treg.ngrok.app/tutorial-import-shell.md` — `treg upload clis` turns the
  CLIs already on your machine into team tools; `treg shell` opens a shell where `stripe`, `gh`, `gcloud` …
  just work with the team key injected. Includes the local-run **security sandbox**.
- **Team access control** → `https://treg.ngrok.app/tutorial-access.md` — choose **which tools each member
  may use** and whether they may run CLIs **locally**, at invite time or any time later.

---

## Concepts (read once)

- **Email is your identity.** You *are* a verified email. Three doors prove it - **GitHub**, an emailed
  **one-time code** (OTP), or an **invite code**. The first time you prove an email you're *registered*;
  every proof after is a *login*. There is no separate sign-up (and no more `treg register`).
- **The proxy = a coat check.** You call the real upstream API *through* the registry. It swaps your tool
  reference for the real secret and injects it server-side. The key never lands on your machine; your
  token authorises the call.
- **A token = a (you, org) pair.** A **User** is an identity; an **Org** is a team that owns resources; a
  **Membership** links them with a role. Your **identity token** (from `treg login`) works across every org
  you belong to - `treg org use <slug>` picks the active one. (Agents/CI can use a per-org token instead.)
- **Invites attach to an email.** An owner/admin invites an *email*. Prove that email (any door) and the
  invite is yours to accept - no code needed. The code is a fast out-of-band shortcut, not a requirement.
- **Tool & skill.** A **tool** = an upstream `base_url` + a list of credential **bindings** (a request may
  carry several). A **skill / bundle** = a recipe (`SKILL.md`) + its secrets + its tool(s), registered from
  a folder via a `treg.json` contract.

### Roles at a glance

| Action | viewer | member | admin | owner |
|---|:--:|:--:|:--:|:--:|
| call tools, read inventory | ✅ | ✅ | ✅ | ✅ |
| register secrets / tools / skills | ❌ | ✅ | ✅ | ✅ |
| edit / delete own resources | ❌ | ✅ | ✅ | ✅ |
| edit / delete any resource in org | ❌ | ❌ | ✅ | ✅ |
| invite / remove members | ❌ | ❌ | ✅ | ✅ |
| change roles, delete org | ❌ | ❌ | ❌ | ✅ |

---

## Setup - simulate three people on one machine

We play three users on one laptop by giving each its own `HOME`, so each gets an isolated
`~/.treg/config.json` pointed at the registry. In real life every person is on their own machine and drops
the `HOME=` prefix.

```bash
for u in tom bob alice; do
  mkdir -p ~/.treg-personas/$u
  HOME=~/.treg-personas/$u treg config --base-url https://treg.ngrok.app
done
```

**Notice:** prefix any command with `HOME=~/.treg-personas/<name>` to act as that person.

---

# Part 1 - Tom founds Superdesign

## Step 1 - Tom signs in (the email door)

There is no `register`. Tom proves his email with a one-time code - and since it's his first time, that same
act **creates** him plus a personal org (so there's never an empty state). The code is **emailed** to him;
he checks his inbox and types it in.

```bash
HOME=~/.treg-personas/tom treg login --email tom@superdesign.dev
```
```
We sent a 6-digit code to tom@superdesign.dev.
Enter code: 429641
✓ Logged in as tom@superdesign.dev. Active org: tom-superdesign-dev
```

**Notice:** Check your inbox for the 6-digit code, then enter it. Tom now holds an **identity token** that
works across every org he joins - no per-org tokens to juggle. (A dev box can set `TREG_EMAIL_DEV_MODE=true`
to print the code inline instead of emailing it.)

## Step 2 - Tom creates the team

His personal org is just his own. Now he spins up the shared team and becomes its **owner**; his active org
switches to it, so everything after runs there.

```bash
HOME=~/.treg-personas/tom treg org create "Superdesign"
```
```json
{
  "org": "superdesign",
  "org_id": 2,
  "name": "Superdesign",
  "role": "owner",
  "token": "<per-org token - for agents/CI; a human doesn't need it>"
}
```

**Notice:** personal orgs are auto-made on sign-in; **teams are created explicitly** with `org create`.

---

# Part 2 - Bob joins via the email door

## Step 3 - Tom invites Bob

Tom invites a teammate by **email**. The invite attaches to that email and Tom gets a one-time code he
*could* hand over - but Bob won't even need it.

```bash
HOME=~/.treg-personas/tom treg org invite bob@superdesign.dev --role member
```
```json
{
  "code": "<one-time-invite-code>",
  "email": "bob@superdesign.dev",
  "role": "member",
  "org_id": 2,
  "expires_at": "2026-07-09T…"
}
```

**Notice:** the invite is pending, addressed to Bob's email, valid 7 days (`--expires-days` to change).

## Step 4 - Bob signs in as himself

Switch to Bob. He proves his email the same way - and since it's his first time, this **creates** him too,
with his own identity token and personal org. He never touches the invite code.

```bash
HOME=~/.treg-personas/bob treg login --email bob@superdesign.dev
```
```
We sent a 6-digit code to bob@superdesign.dev.
Enter code: 512740
✓ Logged in as bob@superdesign.dev. Active org: bob-superdesign-dev
```

**Notice:** same door as Tom - the code lands in Bob's inbox, he enters it. The email is the identity - the
door (GitHub / code) is just how you prove it.

## Step 5 - Bob sees his invite (no code)

Because the invite is tied to Bob's now-proven email, he can just ask what's waiting for him. This is the
full circle: proving the email reveals every invite addressed to it.

```bash
HOME=~/.treg-personas/bob treg invites
```
```json
[
  {
    "id": 1,
    "org": "superdesign",
    "org_id": 2,
    "name": "Superdesign",
    "role": "member",
    "invited_by": "tom@superdesign.dev",
    "expires_at": "2026-07-09T…"
  }
]
```

**Notice:** no code, no copy-paste from Tom - the proven email is the proof.

## Step 6 - Bob accepts

Bob joins Superdesign by naming the org. No code - his proven identity is the proof. His active org switches
to Superdesign.

```bash
HOME=~/.treg-personas/bob treg accept superdesign
```
```json
{
  "org": "superdesign",
  "org_id": 2,
  "name": "Superdesign",
  "role": "member"
}
```

**Notice:** Bob is now a **member** of Superdesign.

## Step 7 - Bob's two hats

One identity, two memberships - owner of his personal org, member of Superdesign. The same identity token
works in both.

```bash
HOME=~/.treg-personas/bob treg org ls
```
```
  bob-superdesign-dev    bob@superdesign.dev    owner
* superdesign            Superdesign            member   (active)
```

**Notice:** the `*` marks the active org. Switch anytime with `treg org use <slug>`.

---

# Part 3 - Alice joins via the code door

## Step 8 - Tom invites Alice as a viewer

The other door: the **code**. First Tom invites Alice as a **viewer** - she'll be able to read and call, but
not register anything.

```bash
HOME=~/.treg-personas/tom treg org invite alice@superdesign.dev --role viewer
```
```json
{
  "code": "ZTeW5ss-cXiyvzeMs3em-…",
  "email": "alice@superdesign.dev",
  "role": "viewer",
  "org_id": 2,
  "expires_at": "2026-07-09T…"
}
```

**Notice:** this time we **keep the code** - Alice uses it directly next.

## Step 9 - Alice joins by code (no login first)

The contrast with Bob: Alice **never runs** `login`. The code itself proves her email, so `join` creates
her, adds her to Superdesign, and saves her token - all in one command.

```bash
HOME=~/.treg-personas/alice treg org join ZTeW5ss-cXiyvzeMs3em-… --email alice@superdesign.dev
```
```json
{
  "org": "superdesign", "org_id": 2, "name": "Superdesign", "role": "viewer",
  "token": "<alice's superdesign token>",
  "personal": { "org": "alice-superdesign-dev", "org_id": 4, "role": "owner",
                "token": "<alice's personal token>" }
}
```

**Notice:** one command created Alice, gave her a personal org, and made her a viewer - Tom never handled
her token.

## Step 10 - The viewer role has teeth

Alice can read and call, but a viewer **cannot register** anything. Watch her get stopped.

```bash
HOME=~/.treg-personas/alice treg secret add testkey --value "nope"
```
```json
{
  "detail": "viewers can call and read, but cannot register"
}
```

**Notice:** Alice was granted *use*, not *write* - the role gate doing its job.

---

# Part 4 - A tool through the proxy

## Step 11 - Bob registers a secret

Unlike Alice, a **member** can register. Bob adds an API key - encrypted server-side, its value never
returned again.

```bash
HOME=~/.treg-personas/bob treg secret add echo-key --value "sk-demo-secret-123"
```
```json
{
  "id": 1,
  "name": "echo-key",
  "kind": "env",
  "owner": "bob@superdesign.dev",
  "bundle_id": null
}
```

**Notice:** the secret is org-scoped (lives in Superdesign) and owned by Bob. A tool binds to it by `id`.

## Step 12 - Bob registers a tool

A tool = an upstream `base_url` + how to inject the credential. We point at postman-echo so we can *see* the
injection. A single `--secret` defaults to a `Bearer` token in the `Authorization` header.

```bash
HOME=~/.treg-personas/bob treg tool add echo --base-url https://postman-echo.com --secret 1
```
```json
{
  "id": 1, "name": "echo", "owner": "bob@superdesign.dev",
  "base_url": "https://postman-echo.com", "host": "postman-echo.com",
  "bindings": [
    { "secret_id": 1, "injector": "env", "location": "header",
      "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token" }
  ]
}
```

**Notice:** for multi-credential upstreams, add more bindings with `--bind` - treg applies every binding on
each call.

## Step 13 - Alice calls it (with no key)

The whole point of treg. Alice is a **viewer** with **no secret** on her machine. Yet when she calls, the
upstream sees Bob's key, injected server-side.

```bash
HOME=~/.treg-personas/alice treg call echo /get
```
```json
{
  "args": {},
  "headers": {
    "host": "postman-echo.com",
    "authorization": "Bearer sk-demo-secret-123",
    "...": "..."
  },
  "url": "https://postman-echo.com/get"
}
```

**Notice:** `authorization: Bearer sk-demo-secret-123` - Bob's secret, which Alice never had, saw, or stored.

## Step 14 - Every call is on the record

The proxy writes an audit row per call. The owner reviews the org's activity.

```bash
HOME=~/.treg-personas/tom treg calls --limit 5
```
```json
[
  {
    "id": 1,
    "user_email": "alice@superdesign.dev",
    "tool_name": "echo",
    "method": "GET",
    "path": "https://postman-echo.com/get",
    "status_code": 200,
    "created_at": "2026-07-02T…"
  }
]
```

**Notice:** even though Alice used Bob's secret, the ledger records **who** actually made the call.

---

# Part 5 - Call shapes & skills

## Step 15 - Call by full URL (agent-native)

An agent often already knows the real upstream URL. Instead of `call <tool> <path>`, hand treg the **whole
URL** - it matches the host to a registered tool and injects the key. No treg-specific knowledge needed.

```bash
HOME=~/.treg-personas/alice treg call https://postman-echo.com/get
```
```
# same echo response, with "authorization": "Bearer sk-demo-secret-123" injected.
# note: no tool name in the command - just the destination URL.
```

**Notice:** treg resolves the tool by **host**, so the agent-native full-URL form just works.

## Step 16 - The raw HTTP underneath

`treg call` is sugar. Under the hood it's a plain HTTP request to `<proxy>/call/<upstream-url>` with your
token header - any language, any agent, `curl`.

```bash
ATOK=$(python3 -c "import json;print(json.load(open('/Users/you/.treg-personas/alice/.treg/config.json'))['token'])")
curl -s -H "X-Treg-Token: $ATOK" \
  "https://treg.ngrok.app/call/https://postman-echo.com/get"
```
```
# the postman-echo JSON again, "authorization": "Bearer sk-demo-secret-123" injected -
# just curl, no secret on the client.
```

**Notice:** the whole product in one line: prefix any upstream URL with the proxy, send your token, treg
swaps in the real credential.

## Step 17 - Draft a skill's registration

A whole skill folder (a recipe + credential files) can register in one shot via a `treg.json` contract.
`skill init` scans `SKILL.md` + the `.secret/` dir and drafts it - guessing the base URL and finding the
secret. No values go in the file, only references.

```bash
HOME=~/.treg-personas/bob treg skill init --dir /tmp/skills/echo-svc
```
```
wrote /tmp/skills/echo-svc/treg.json
  auto: base_url=https://postman-echo.com | secrets=['echo-svc']
  review / fill:
    - base_url - heuristic guess, verify
    - health / examples - optional
```

**Notice:** it read the recipe and correctly guessed `base_url` + found the secret - fix anything it flagged,
then register.

## Step 18 - Upload the whole skill

One command turns the folder into a live tool: the recipe, the secret (value loaded from `.secret/`, never
the json), and the tool - all created atomically as a **bundle**.

```bash
HOME=~/.treg-personas/bob treg skill add --dir /tmp/skills/echo-svc
```
```json
{
  "id": 1, "name": "echo-svc", "owner": "bob@superdesign.dev",
  "recipe": "# echo-svc\n…the SKILL.md…",
  "tools":   [{ "id": 2, "name": "echo-svc", "base_url": "https://postman-echo.com", "bundle_id": 1 }],
  "secrets": [{ "id": 2, "name": "echo-svc", "kind": "env", "bundle_id": 1 }]
}
```

**Notice:** everything shares a `bundle_id`, so a skill deletes as one unit too.

---

# Part 5b - Import: the magic bulk on-ramp

Everything above, but for your **whole environment at once**. This is the fastest way to fill a team's
registry.

## Step 18a - Turn your whole `.env` into tools

`treg upload env` reads your `.env`, matches each variable against a catalog of ~80 providers, and
registers the ones you pick as ready-to-call tools. Detection reads **names only**; the value is loaded
only for the keys you confirm. Config vars (`*_HOST`, `*_MODEL`, `*_PROJECT_ID`) and your app's own
secrets (`SECRET_KEY`, `SESSION_SECRET`, `DATABASE_URL`, `*_WEBHOOK_SECRET`) are excluded automatically.

```bash
HOME=~/.treg-personas/bob treg upload env --select openai,stripe,resend
```
```text
Scanned .env: 6 key(s) to register, 1 OAuth, 4 other.
  ✓ openai         https://api.openai.com/v1   [Authorization: Bearer {secret}]
  ✓ stripe         https://api.stripe.com/v1    [Authorization: Bearer {secret}]
  ✓ resend         https://api.resend.com       [Authorization: Bearer {secret}]

Registered 3/3 tools.
```

**Notice:** a `CLIENT_ID`+`CLIENT_SECRET` pair is detected as **OAuth** and offered a guided
`treg oauth connect` instead of a broken bearer key. Other auth shapes are handled too - an API-key
header (`x-api-key`), a query-param key (`?apiKey=`), or a Basic pair (base64 `id:secret`).

## Step 18b - Import a whole folder of skills

Point `treg upload skills` at a directory of skills. For each, it uses an existing `treg.json`, or
**builds one** from the skill's script (base URL + the env var it reads) - registering API skills as
tools and knowledge skills as recipe-only bundles. The whole team library lands in one pass.

```bash
HOME=~/.treg-personas/bob treg upload skills --dir ~/.claude/skills --all
```
```text
Scanned ~/.claude/skills: 5 API-tool skill(s), 23 recipe-only.
  ✓ render          (tool)   [wrote treg.json]
  ✓ intercom        (tool)
  ✓ seo-blog-writer (recipe)
  …
Imported 27/28 skills.
```

Re-run any time - it skips what's already registered (or `--replace` to update). A teammate then pulls
any of them with `treg skill install <name>` (or `--all`), which writes the recipe into their
`.claude/skills/`. Bare `treg upload` (no `env`/`skills`) does **both** for the current directory.

> **Non-interactive safety:** run from an agent/CI (no TTY) and upload refuses without `--all` or
> `--select`, so credentials are never registered unattended by accident.

---

# Part 6 - Org administration

## Step 19 - See the team

The owner lists everyone and their roles. Role changes reference a member by `user_id`.

```bash
HOME=~/.treg-personas/tom treg org members
```
```json
[
  { "user_id": 1, "email": "tom@superdesign.dev",   "role": "owner"  },
  { "user_id": 2, "email": "bob@superdesign.dev",   "role": "member" },
  { "user_id": 3, "email": "alice@superdesign.dev", "role": "viewer" }
]
```

**Notice:** the full roster from one command.

## Step 20 - Promote Bob to admin

Only an owner changes roles. Let's make Bob an **admin** - he can invite/manage, but transfer and delete
stay owner-only. The last-owner guard stops an org from becoming ownerless.

```bash
HOME=~/.treg-personas/tom treg org set-role 2 admin
```
```json
{
  "user_id": 2,
  "role": "admin",
  "org_id": 2
}
```

**Notice:** one primitive (`set-role`) covers promotion, demotion, and ownership transfer.

## Step 21 - Admin rights in action

As a plain member Bob couldn't invite; as an **admin** he can. He invites a new teammate.

```bash
HOME=~/.treg-personas/bob treg org invite dana@superdesign.dev --role member
```
```json
{
  "code": "<one-time-code>",
  "email": "dana@superdesign.dev",
  "role": "member",
  "org_id": 2,
  "expires_at": "2026-07-09T…"
}
```

**Notice:** Bob manages the team without being the owner.

## Step 22 - Review pending invites

Admins see every invite still outstanding for the org. Accepted, revoked, and expired ones are filtered out.

```bash
HOME=~/.treg-personas/bob treg org invites
```
```json
[
  {
    "id": 3, "email": "dana@superdesign.dev", "role": "member",
    "invited_by": "bob@superdesign.dev", "expires_at": "2026-07-09T…"
  }
]
```

**Notice:** only Dana shows - Bob's and Alice's invites are already accepted, so they're gone from the list.

## Step 23 - Revoke an invite

Plans change - Bob kills Dana's invite before she uses it. This hard-deletes the code so it can never be
accepted.

```bash
HOME=~/.treg-personas/bob treg org revoke 3
```
```json
{
  "revoked_invite": 3
}
```

**Notice:** at join time: expired → `410`; revoked / used / unknown → `404 invalid or already-used invite`.

## Step 24 - The role gate, from the viewer side

Alice is a viewer. She can call tools, but she can't invite - that needs admin+. She gets refused.

```bash
HOME=~/.treg-personas/alice treg org invite eve@superdesign.dev --role member
```
```json
{
  "detail": "admin role in this org is required"
}
```

**Notice:** roles, cleanly enforced: **owner** > **admin** > **member** > **viewer**.

---

# Part 7 - Super-admin

## Step 25 - Become the platform operator

Super-admin sits *above* orgs - it reads and manages every tenant. Two ways to authorise: the platform
bearer `TREG_ADMIN_TOKEN`, or a user flagged `is_superadmin`. We use the bearer, read from `.env` so it
never appears on screen.

```bash
treg admin login --token "$(grep -E '^TREG_ADMIN_TOKEN=' .env | cut -d= -f2-)"
```
```
admin token saved
```

**Notice:** gated by `require_superadmin`, separate from org roles: a normal token → 403, no token → 401.

## Step 26 - The whole platform at a glance

One call gives totals across every tenant - the picture no single org owner can see. Plus
`admin orgs / users / tools / health` for cross-tenant inventory.

```bash
treg admin stats
```
```json
{
  "totals": { "users": 3, "orgs": 4, "tools": 2, "secrets": 2, "calls": 1 },
  "...recent-activity + distributions...": "..."
}
```

**Notice:** portal-ready JSON: distributions by injector/host, a credential-health rollup, call volume, and
growth counts.

## Step 27 - Every org, across all tenants

Cross-tenant visibility: Superdesign with its members + tools, plus everyone's personal orgs.

```bash
treg admin orgs
```
```json
[
  { "id": 2, "slug": "superdesign", "name": "Superdesign", "members": 3, "tools": 2 },
  { "id": 1, "slug": "tom-superdesign-dev", "members": 1, "tools": 0 },
  { "id": 3, "slug": "bob-superdesign-dev", "members": 1, "tools": 0 },
  { "id": 4, "slug": "alice-superdesign-dev", "members": 1, "tools": 0 }
]
```

**Notice:** the seam a support console or billing portal sits on later - same JSON, just rendered.

## Step 28 - Grant a real user super-admin

The env bearer bootstraps; then you promote named users so they reach `/admin/*` with their own identity
token - no shared secret to pass around.

```bash
treg admin grant 1
```
```json
{
  "user_id": 1,
  "is_superadmin": true
}
```

**Notice:** after the grant, Tom's normal identity token works on `admin` commands - and the dashboard's
Admin panel lights up for him.

---

# Part 8 - The dashboard

## Step 29 - The same registry, in the browser

Open **treg.ngrok.app** and sign in with the **email code** door (the same one you used in the terminal):
type your email → click **Email me a sign-in code** → **check your inbox** for the 6-digit code → paste it
in and **Sign in**. You land on your team org - Tools shows the `echo` tool, Activity shows the call, and
(since Tom is now super-admin) an **Admin** panel appears.

```bash
open https://treg.ngrok.app/
```
```
# Sign in with email → land on Superdesign
#   Tools    → the echo tool (Copy a snippet · Try it live)
#   Activity → Alice's GET echo · 200
#   Admin    → cross-tenant stats + orgs (super-admin only)
```

**Notice:** the dashboard is read + call today; creating/inviting/registering stays in the CLI (dashboard
write UI is Phase 2).

---

# Part 9 - Cleanup

## Step 30 - Delete the tool

Bob (its creator, and an admin) removes the tool. The bound secret stays - only the tool goes. A member
can't delete a teammate's resource.

```bash
HOME=~/.treg-personas/bob treg tool rm 1
```
```json
{
  "deleted": 1
}
```

**Notice:** delete order matters: remove the tool (or its binding) before the secret it uses.

## Step 31 - Delete the org (full cascade)

The finale. Deleting an org is owner-only and **confirm-by-name** - you must type the slug, and it must be
your active org. The cascade removes all memberships, tools, secrets, bundles, invites, and audit rows.

```bash
HOME=~/.treg-personas/tom treg org delete superdesign
```
```json
{
  "deleted_org": 2
}
```

**Notice:** Bob and Alice keep their personal orgs - they were separate tenants all along. That's the full
lifecycle: sign in → team → invite (both doors) → roles → tool → proxied call → audit → admin → tear down. 🏁

---

## Appendix A - the four auth shapes

| Shape | Who | How | Header(s) |
|---|---|---|---|
| **Identity token** | humans on the CLI | `treg login` (GitHub) or `treg login --email` (OTP) | `X-Treg-Token: <identity>` + `X-Treg-Org: <slug>` |
| **Per-org token** | agents / CI | baked into an org at `org create` / `org join` | `X-Treg-Token: <org-token>` (org is implicit) |
| **Session cookie** | the dashboard | GitHub or email-code sign-in sets a signed HttpOnly cookie | cookie + `X-Treg-Org: <slug>` |
| **Admin bearer** | platform operator | `TREG_ADMIN_TOKEN`, or a user flagged `is_superadmin` | `X-Treg-Token: <admin>` on `/admin/*` |

## Appendix B - command reference

```
treg login [--email you@x.com | --token <t>]   # sign in (GitHub default; email OTP; or a raw token)
treg logout                                     # clear your credentials
treg invites                                    # invites addressed to you (code-free)
treg accept <org-slug>                          # accept one addressed to you (no code)

treg org create "<Name>"                        # make a team, become owner
treg org ls | org use <slug>                    # list / switch active org
treg org invite <email> [--role viewer|member|admin] [--expires-days N]
treg org join <code> --email you@x.com          # the code door (creates you if new)
treg org members | set-role <user_id> <role>    # roster / change a role (owner)
treg org invites | revoke <invite_id>           # list / kill pending invites (admin+)
treg org leave | delete <slug>                  # self-remove / delete (owner, confirm-by-name)

treg secret add <name> (--value V | --file F | --dir D) [--kind env|oauth|...]
treg tool add <name> --base-url URL (--secret ID | --bind '...' | --binding '<json>')
treg secret ls | rm ID | update ID …            # tool ls | rm | update likewise
treg call <tool> <path>  |  call <full-url>     # proxy a call (named or agent-native)
treg calls [--limit N]                          # the audit log

treg skill init --dir D | skill add --dir D | skill ls | skill rm ID
treg admin login --token T | stats | orgs | users | tools | health | grant/revoke ID | …

Global: --org <slug> runs any single command in that org instead of the active one.
```

The `register` command is retired - `login` is register-or-login. Multi-user demos use the **invite path**
(`org invite` → `login`/`invites`/`accept`, or `org join <code>`).
