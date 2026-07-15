/* Dashboard-tour data — one source for both the standalone page (tour/index.html) and the
 * native in-dashboard view (web/index.html). window.TREG_TOUR.steps = [{part,who,img,title,explain,notice}].
 * Images live beside this file at ./img/<img> (served at /dashboard-tour/img/<img>). */
(function () {
  window.TREG_TOUR = {
    personas: { tom:"Tom · owner", bob:"Bob · member→admin", alice:"Alice · viewer", sys:"sign-in" },
    colors: ["--accent","--green","--amber","--teal","--red"],  // one mat colour per Part, cycled
    steps: [
      { part:"Sign in", who:"sys", img:"01-signin.webp", title:"Three ways in",
        explain:"The sign-in screen offers three separate doors — pick <b>one</b>: <b>Continue with GitHub</b> (real OAuth), <b>Email me a sign-in code</b> (works for any email), or the collapsible <b>paste an org token</b> (for agents & the CLI).",
        notice:"This tour uses the email-code door. In production the code is <b>emailed</b> to you (via Resend); the screenshots here were captured in dev mode, where the code is shown on the page instead." },
      { part:"Sign in", who:"sys", img:"02-email-code.webp", title:"The email code",
        explain:"Type your email and click <b>Email me a sign-in code</b>. <b>Check your inbox</b> for the 6-digit code, paste it into the field, and <b>Sign in</b>. First sign-in also registers you. <span class=\"muted\">(In this dev-mode screenshot the code shows inline as <code>dev code 619565</code>; in production it arrives by email instead.)</span>",
        notice:"Check your email, enter the code, Sign in. A brand-new email creates the user + a personal org — no separate sign-up." },
      { part:"Sign in", who:"tom", img:"03-dashboard-empty.webp", title:"Your dashboard",
        explain:"You land on your <b>active org</b>. A fresh account has a personal org and no tools. The header carries the org switcher, a theme toggle, your avatar and Sign out; the left nav has Tools, Organizations, Activity, and Tutorial.",
        notice:"Everything is scoped to the active org shown in the header — switch it anytime from the org menu." },

      { part:"Build a team", who:"tom", img:"04-create-team.webp", title:"Create a team",
        explain:"Your personal org is just yours. Click <b>+ New team</b> and name it — you become its <b>owner</b> and it becomes active. Teams are where you share tools.",
        notice:"Personal orgs are made automatically on sign-in; teams are always created explicitly." },
      { part:"Build a team", who:"tom", img:"05-manage-panel.webp", title:"You're the owner",
        explain:"On the <b>Organizations</b> page your active team gets a <b>Manage</b> panel: members, invites, and a danger zone. Right now it's just you.",
        notice:"The Manage panel only shows for teams (not your personal org); its member/invite tools appear for admins and owners." },

      { part:"Bring people in", who:"tom", img:"06-invite.webp", title:"Invite a teammate",
        explain:"Enter a teammate's email, pick a role (viewer / member / admin), and <b>Invite</b>. treg mints a <b>one-time code</b> shown inline (with a copy button). They can also accept it code-free — the invite is tied to their email.",
        notice:"Below, <b>Pending invites</b> lists everything outstanding; <b>Revoke</b> kills a code before it's used." },
      { part:"Bring people in", who:"bob", img:"08-invite-banner.webp", title:"Accept via banner",
        explain:"When the invited person signs in, the invite shows up as a <b>banner</b> at the top — one click on <b>Accept</b> and they're in. No code to copy or paste.",
        notice:"Invites attach to your email, so proving that email (any door) is enough to accept — the code is just an out-of-band shortcut." },
      { part:"Bring people in", who:"alice", img:"17-join-by-code.webp", title:"…or join by code",
        explain:"If someone handed you a code out-of-band, open <b>⤷ Join by code</b> on the Organizations page and paste it. It must match your email.",
        notice:"Two doors into a team: the automatic banner, or a pasted code — same result." },

      { part:"Register resources", who:"bob", img:"09-secrets.webp", title:"Add a secret",
        explain:"A <b>member</b> (or higher) can register. Open the <b>⚿ Secrets</b> panel and add a credential — a name, its value, and a kind. The value is encrypted server-side and never shown again.",
        notice:"Secrets are the credentials your tools inject. Viewers don't see this panel at all." },
      { part:"Register resources", who:"bob", img:"10-add-tool.webp", title:"Register a tool",
        explain:"Click <b>+ Add tool</b>. A tool is an upstream <b>base URL</b> plus a <b>binding</b> — pick the secret, where it goes (header or query param), the field name, and a format like <code>Bearer {secret}</code>.",
        notice:"<code>{secret}</code> is replaced with the real credential at call time — the caller never holds it." },
      { part:"Register resources", who:"bob", img:"11-multi-binding.webp", title:"Multi-credential tools",
        explain:"Some upstreams need more than one credential per request. Click <b>+ binding</b> to add another row — every binding is applied on each call (e.g. an OAuth bearer <i>and</i> a developer-token header).",
        notice:"Same builder; one tool can carry any number of bindings." },
      { part:"Register resources", who:"bob", img:"12-edit-tool.webp", title:"Edit a tool",
        explain:"The ✎ button on a tool card reopens the builder pre-filled, so you can change the base URL or add/remove bindings. Saving sends a PATCH — the tool's name stays fixed.",
        notice:"Delete is the ✕ on the card (inline confirm); a secret still bound by a tool is protected from deletion." },
      { part:"Register resources", who:"bob", img:"13-skill.webp", title:"Register a skill (bundle)",
        explain:"A <b>skill</b> = a recipe + its secrets + its tool(s), registered atomically as a <b>bundle</b>. Click <b>+ Skill</b> and paste the payload (secret values go inline here; the CLI's <code>treg.json</code> loads them from files instead).",
        notice:"Bindings reference a secret by its <code>local_name</code>; the JSON is validated before it's sent." },
      { part:"Register resources", who:"bob", img:"14-tools-list.webp", title:"Your tools",
        explain:"Registered tools appear as cards — upstream host, injectors in play, owner, and a health badge. From each card you can Copy a snippet, Try it live, edit, or delete.",
        notice:"Ownership is kept for audit, but everyone in the org can see and call every tool." },

      { part:"Use tools", who:"alice", img:"15-try-it.webp", title:"Try it — key injected",
        explain:"Open <b>Try it</b> on any tool, type the upstream path, and Send. The call runs through the proxy with the credential injected server-side — the response shows <code>authorization: Bearer …</code> even though you never sent a key.",
        notice:"This is the whole product: call the real API through treg, and the secret is added on the server. Even a <b>viewer</b> can do this." },
      { part:"Use tools", who:"alice", img:"16-copy.webp", title:"Copy a snippet",
        explain:"Prefer to call from code? <b>Copy for…</b> gives a ready snippet for Claude Code, the CLI, Python, Node, or cURL — all pointed at the proxy with your token, no key inlined.",
        notice:"The snippet uses the public proxy domain, so it's shareable as-is." },

      { part:"Roles", who:"alice", img:"18-viewer-tools.webp", title:"The viewer role",
        explain:"Signed in as a <b>viewer</b>, Alice sees the tools and can <b>Copy</b> and <b>Try it</b> — but the register controls (Secrets, + Skill, + Add tool) and the Manage panel are simply gone. Use without the ability to change credentials.",
        notice:"Roles: owner > admin > member > viewer. The UI hides what your role can't do; the server enforces it too." },
      { part:"Roles", who:"tom", img:"20-danger-zone.webp", title:"Manage the team",
        explain:"Back as the owner, the Manage panel now lists everyone. Owners change roles from the dropdown, admins can <b>Remove</b> members, and the <b>danger zone</b> has Leave (with a last-owner guard) and Delete (type the name to confirm).",
        notice:"Destructive actions use inline confirmations — no surprise clicks." },

      { part:"Super-admin", who:"tom", img:"22-admin-users.webp", title:"Platform control",
        explain:"A super-admin gets an <b>Admin</b> nav with cross-tenant reach: platform stats, every org (suspend / delete), and every user (grant/revoke super-admin, suspend, delete). Your own row is guarded so you can't lock yourself out.",
        notice:"The Admin nav only appears for super-admins; everyone else never sees it." },

      { part:"Wrap up", who:"tom", img:"23-activity.webp", title:"Activity — the audit log",
        explain:"<b>Activity</b> lists every call made through the proxy in this org — who called what, when, and the status. Even when someone uses a teammate's key, the ledger records the real caller.",
        notice:"Accountability without sharing secrets: that's the point of the proxy." },
      { part:"Wrap up", who:"tom", img:"24-help-tutorial.webp", title:"The in-app tutorial",
        explain:"Under <b>Help → Tutorial</b>, the dashboard ships the full interactive CLI walkthrough too — so the terminal and the browser are always documented in the same place.",
        notice:"That's the whole UI: sign in → build a team → invite → register → call → administer → tear down. 🏁" },
    ],
  };
})();
