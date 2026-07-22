---
name: linkedin
description: Post to LinkedIn as the connected member, and read their profile, through treg. Credentials are injected server-side; you never hold a token. Use when asked to publish a LinkedIn post, draft and share an update, or look up the connected member's own profile.
---

# LinkedIn via treg

You post **as the member who connected the account** — their own profile, not a company page.
treg injects the OAuth token server-side; you never see a credential.

## Setup (once)

```bash
treg oauth connect --provider linkedin   # opens browser consent
treg connections ls                      # check health + expiry
```

## 🔴 The connection dies after ~60 days, silently

LinkedIn issues **no refresh token** at the non-partner tier. treg cannot renew it. The token simply
stops working roughly 60 days after consent, with no warning from LinkedIn.

treg tracks this: the connection shows `manual renew`, and `needs_reconnect` flips true as expiry
approaches. **If a call fails with 401, check `treg connections ls` before debugging anything else** —
the fix is reconnecting, not retrying.

## Who am I posting as?

Every post needs the member's URN, which you get from the OpenID userinfo endpoint:

```bash
treg call linkedin v2/userinfo
# → {"sub": "abc123XYZ", "name": "...", "email": "..."}
```

The `sub` value is the member id. The author URN is `urn:li:person:<sub>`.

## Posting

⚠️ **Two headers are required and treg does not add them** — it relays your headers verbatim, so
you must send them:

| Header | Value |
|---|---|
| `X-Restli-Protocol-Version` | `2.0.0` |
| `LinkedIn-Version` | `202603` (YYYYMM; LinkedIn deprecates versions ~3 months out) |

**Text post:**

```bash
treg call linkedin rest/posts --method POST \
  --content-type application/json \
  --data '{
    "author": "urn:li:person:abc123XYZ",
    "commentary": "Your post text here",
    "visibility": "PUBLIC",
    "distribution": {"feedDistribution": "MAIN_FEED"},
    "lifecycleState": "PUBLISHED"
  }'
```

Send the two headers above with the request.

A successful post returns **`201` with an empty body**; the post id is in the `x-restli-id`
response header, not the body. Don't treat the empty body as a failure.

## Gotchas that will bite you

- **Query params need `--query`, not `?` in the path.** `treg call linkedin "path?x=1"` silently
  drops the query string and returns a plausible-looking wrong answer.
- **`commentary` escaping**: LinkedIn treats `(`, `)`, `[`, `]`, `{`, `}`, `<`, `>`, `@`, `|`, `~`,
  `_` and `*` as reserved in post text. Escape them with a backslash or the post is rejected.
- **You can only post as the connected member.** Company/organization posting needs the Community
  Management API on a company-verified app — a different LinkedIn app entirely, not a scope you
  can add here.
- **LinkedIn rate-limits per member per day.** A burst of posts will start failing; space them out.
- **Don't repost the same text.** LinkedIn's spam detection acts on duplicate content across
  accounts, and the penalty lands on the member's profile, not on us.

## Before you publish

**Always show the user the exact post text and get explicit confirmation before calling.** This
publishes publicly under their real name and professional identity — there is no meaningful undo,
and a bad post is visible to their network immediately.
