# tools-registry — team access control (hands-on)

Decide **which tools each member may use**, and whether they may run CLIs **on their own machine**. This
tutorial follows the same format as the [main tutorial](/tutorial): every step shows the **exact command**,
the **expected output**, and **what to notice** — so it reads standalone. Copy each command and follow along.

We use the registry at `https://treg.superdesign.dev`.

---

## Concepts (read once)

- **Two independent dials per member.**
  - **Tool access** (`tool_access`): which tools this member may touch. The default is **all tools** — nobody
    is restricted until you decide to be. Set it to an explicit **allow-list** of tool names and the member
    may use *only* those.
  - **Local execution** (`local_run_enabled`): may this member run a CLI **on their own machine**
    (`treg run`, the default local tier)? On by default. Turn it **off** and they can still run on the
    server (`treg run --server`), but never locally.
- **Access gates every way of using a tool.** A withheld tool is unreachable through *all* doors: the HTTP
  proxy `treg call`, the server run `treg run --server`, and the local run `treg run`. There is no side path.
- **The owner is never restricted.** An owner always has every tool and local runs — they are the team's
  authority. **Admins and members** can both be restricted (the dial is per person, not per role).
- **It is an explicit allow-list.** A member on "all tools" automatically gains access to any *new* tool you
  register later. A member with a **customized** list does **not** — an admin adds the new tool to them by
  hand. (The dashboard shows a reminder when you register a tool and someone is customized.) Tip: if you
  tick *every* tool in the picker, it collapses back to "all", so that member keeps auto-getting new tools.
- **Set at invite time, change any time.** You choose a new member's access in the invite; you can widen or
  narrow it later. Nothing is permanent.

### What each dial does

| Situation | `treg call <tool>` | `treg run <tool>` (local) | `treg run --server <tool>` |
|---|:--:|:--:|:--:|
| tool **in** the member's access (or access = all) | ✅ | ✅ (if local on) | ✅ |
| tool **not** in the member's access | ❌ blocked | ❌ blocked | ❌ blocked |
| `local_run_enabled` = **off** | ✅ | ❌ blocked | ✅ |
| the member is the **owner** | ✅ always | ✅ always | ✅ always |

---

## Setup — play two people on one machine

Like the main tutorial, we give each persona its own `HOME`, so each has an isolated
`~/.treg/config.json` pointed at the registry. In real life every person is on their own machine and drops
the `HOME=` prefix.

```bash
for u in tom sam; do
  mkdir -p ~/.treg-personas/$u
  HOME=~/.treg-personas/$u treg config --base-url https://treg.superdesign.dev
done
```

**Notice:** prefix any command with `HOME=~/.treg-personas/<name>` to act as that person. **Tom** is the team
owner (already signed in to his team, with tools like `gh` and `stripe` registered). **Sam** is the new
teammate we will restrict.

---

# Part 1 — Invite a member with tailored access

## Step 1 — Tom invites Sam, restricted to one tool, no local runs

Tom invites Sam by email, but grants access to **only** the `gh` tool and turns **local runs off**. The
access travels with the invite and lands on Sam's membership the moment he accepts.

```bash
HOME=~/.treg-personas/tom treg org invite sam@superdesign.dev --tools gh --local-run off
```
```json
{
  "code": "<one-time-invite-code>",
  "email": "sam@superdesign.dev",
  "role": "member",
  "org_id": 44,
  "expires_at": "2026-07-21T…"
}
```

**Notice:** `--tools gh` is the allow-list (comma-separated for several, e.g. `--tools gh,stripe`);
`--local-run off` means server-only. The invite prints a one-time code, but Sam can also accept it just by
proving his email — see the main tutorial's "code-free" door.

## Step 2 — (Alternative) let treg ask

If Tom runs the invite **without** the access flags, treg asks him interactively — "give access to all
tools?" — and, if he says no, shows a checklist of every tool (all pre-ticked) to uncheck the ones to
withhold.

```bash
HOME=~/.treg-personas/tom treg org invite sam@superdesign.dev
```
```
Give access to all 10 tools? [Y/n]: n
? Tools this member may use  (↑↓ move, space toggle, enter confirm)
 ◉ gh
 ◯ stripe
 ◉ gcloud
 …
```

**Notice:** `--all-tools` skips the prompt and grants everything. The checklist is the same idea as the
dashboard's "Customize" (Part 4).

## Step 3 — Sam accepts

Sam proves his email (any door), then accepts. His membership is created **carrying** the access from the
invite: only `gh`, no local runs.

```bash
HOME=~/.treg-personas/sam treg login --email sam@superdesign.dev
HOME=~/.treg-personas/sam treg accept superdesign
```
```json
{
  "org": "superdesign",
  "org_id": 44,
  "name": "Superdesign",
  "role": "member"
}
```

**Notice:** Sam is a **member** — but a *restricted* one. The next part shows the walls.

---

# Part 2 — The restricted member hits the walls

## Step 4 — An allowed tool works

`gh` is on Sam's list, so calling it through the proxy is fine (the key is injected server-side; nothing
lands on Sam's machine).

```bash
HOME=~/.treg-personas/sam treg call gh zen
```
```
Keep it logically awesome.
```

**Notice:** no error — Sam has access to `gh`, so the proxy serves the call normally.

## Step 5 — A withheld tool is blocked

`stripe` is **not** on Sam's list. Every door to it is closed — here, the proxy.

```bash
HOME=~/.treg-personas/sam treg call stripe v1/balance
```
```json
{
  "detail": "you don't have access to the tool 'stripe' in this team — an admin can grant it (dashboard → Team, or `treg org access <you> --tools …`)"
}
```

**Notice:** the message tells Sam exactly how to get access. Because access gates *all* doors, `treg run
stripe` and `treg run --server stripe` are refused the same way.

## Step 6 — Local execution is off

`gh` is allowed, but Sam's `local_run_enabled` is **off**, so he cannot run it on his own machine. treg
points him at the server tier instead.

```bash
HOME=~/.treg-personas/sam treg run gh -- --version
```
```
treg: local execution is disabled for you — run on the server instead (`treg run --server`), or ask an admin to enable local runs for your account
```

**Notice:** this is the *local* wall, separate from tool access. `gh` is allowed — Sam just can't run it
**locally**. `treg run --server gh …` runs on the registry (where the key stays) and is allowed.

## Step 7 — A member can't manage the team

Access control is an admin power. Sam (a member) cannot list or change anyone's access.

```bash
HOME=~/.treg-personas/sam treg org members
```
```json
{
  "detail": "admin role in this org is required"
}
```

**Notice:** only admins and the owner see the roster and edit access. Sam can only be *given* access, not
grant it.

---

# Part 3 — The owner adjusts access later

## Step 8 — Tom sees everyone's access

The members list now shows each person's `tool_access` (their allow-list, or `null` = all) and
`local_run_enabled`.

```bash
HOME=~/.treg-personas/tom treg org members
```
```json
[
  {
    "user_id": 60,
    "email": "tom@superdesign.dev",
    "role": "owner",
    "daily_call_cap": -1,
    "used_today": 3,
    "tool_access": null,
    "local_run_enabled": true
  },
  {
    "user_id": 63,
    "email": "sam@superdesign.dev",
    "role": "member",
    "daily_call_cap": -1,
    "used_today": 1,
    "tool_access": ["gh"],
    "local_run_enabled": false
  }
]
```

**Notice:** Tom (owner) is `"tool_access": null` — all tools, always. Sam is `["gh"]` with local off, exactly
as invited.

## Step 9 — Tom widens Sam's access

Tom gives Sam **all** tools and turns local runs **on**. Use Sam's `user_id` from the list above.

```bash
HOME=~/.treg-personas/tom treg org access 63 --all-tools --local-run on
```
```json
{
  "user_id": 63,
  "org_id": 44,
  "tool_access": null,
  "local_run_enabled": true
}
```

**Notice:** `--all-tools` clears the list (`tool_access` → `null` = all). To set a specific list instead,
use `--tools a,b`. A flag you *don't* pass keeps its current value — so `treg org access 63 --local-run off`
alone flips only the local dial and leaves the tool list untouched.

## Step 10 — Sam tries again — now everything works

```bash
HOME=~/.treg-personas/sam treg call stripe v1/balance
```
```json
{ "object": "balance", "available": [ … ] }
```
```bash
HOME=~/.treg-personas/sam treg run gh -- --version
```
```
▸ gh · audit #58
gh version 2.72.0 (2025-04-30)
```

**Notice:** `stripe` is reachable now (Sam has all tools), and `gh` runs locally (local is on). The same two
dials, opened up.

## Step 11 — Narrow again, precisely

Access is fluid. Tom can pin Sam to an exact set at any time:

```bash
HOME=~/.treg-personas/tom treg org access 63 --tools gh,gcloud
```

**Notice:** an **empty** result of the picker (no tools) is valid too — it means "no tools", which
effectively switches the CLI/proxy off for that member while keeping them in the team. Unknown tool names
are rejected with a clear `422` so you never grant a typo.

---

# Part 4 — The same controls in the dashboard

Everything above is also point-and-click at `https://treg.superdesign.dev/` → **Team**.

- **The members table** gains two cells per person:
  - **Tools** — shows `All` or `N tools`. Click it to open a checklist of *every* tool in the team;
    ticked = allowed. Save writes the new access. Ticking all collapses back to `All`.
  - **Local run** — an on/off toggle. Turning it off is the same as `--local-run off`.
  The **owner's** row shows `All` and its controls are disabled (an owner is never restricted).
- **The invite box** offers **All tools** / **Customize** and a **Local runs allowed** switch. "Customize"
  reveals the same tool checklist (all pre-ticked). This is the click version of Step 1.
- **A reminder toast.** When you register a **new** tool and at least one member has a *customized* list, a
  toast reminds you: *"N member(s) have a customized selection and won't see it until you add it."* — because
  new tools reach "all-tools" members automatically, but not customized ones.

---

# Part 5 — The API underneath (agents / CI)

The CLI and dashboard both drive two endpoints — handy if an agent manages the team:

- **Set a member's access:**
  ```
  PATCH /orgs/{org_id}/members/{user_id}/access
  { "tool_access": ["gh","stripe"] | null, "local_run_enabled": true }
  ```
  `null` = all tools; a list = the allow-list (validated against the org — unknown names → `422`; ticking
  every tool collapses to `null`). Admin/owner only; an owner cannot be restricted.
- **Read it:** `GET /orgs/{org_id}/members` returns `tool_access` + `local_run_enabled` on every member.
- **Seed it at invite:** `POST /orgs/{org_id}/invites` accepts `tool_access` + `local_run_enabled`; they are
  copied onto the membership when the invite is accepted (either door).

---

## How this was tested

This exact flow was verified live through the CLI before release: a member restricted to `gh` with local
off was **blocked** on `stripe` (proxy) and on any **local** run, **allowed** on `gh`, and — after the owner
ran `treg org access … --all-tools --local-run on` — everything opened up. On top of that walk-through, the
feature carries automated tests proving the invariants: the **owner is never restricted**, **admins can be**,
a **viewer is tool-gated on their calls**, an **empty list blocks every tool**, an **unknown tool is
rejected (422)**, ticking **all tools collapses to "all"**, and an **invite carries its access onto the
membership**. The two new database columns were confirmed to add cleanly to an existing Postgres database.

---

## See also

- The full [main tutorial](/tutorial) — sign-in, teams, the proxy, calls, skills.
- [Import & shell mode](/tutorial-import-shell.md) — bulk-register your CLIs and use them transparently.
