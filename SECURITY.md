<!-- DRAFT — review and set the contact address before publishing. -->

# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue.

Use GitHub's **[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
(the "Report a vulnerability" button on the Security tab), or email **<security@REPLACE-ME>**.

We aim to acknowledge a report within a few business days, agree on a disclosure timeline, and credit
reporters who wish to be named.

## The security model (what protects your keys)

tools-registry is built so that **reading the source does not help an attacker** — nothing load-bearing
is hidden in the code. Secrets live in the database (encrypted with a Fernet key held only in the server
environment) and enforcement happens server-side and in the operating system.

- **The proxy never hands the key to the caller.** For an HTTP tool, the registry injects the credential
  server-side and makes the upstream call; the consumer's token only authorizes it.
- **Encryption at rest.** Stored secrets are Fernet-encrypted; the key is an environment variable, never
  in the repo or the database.
- **Tenant isolation.** Every secret, tool, and record is scoped to an org; access is gated by role and,
  per member, by an explicit tool allow-list.
- **SSRF guard.** A tool's upstream host is re-resolved at call time and internal/metadata addresses are
  refused (defeats DNS-rebinding).
- **Local runs are sandboxed.** `treg run` on a member's machine executes the CLI as a locked-down
  `treg-run` user, with an egress allow-list and an optional filesystem jail, so a shared key can't be
  read, exfiltrated over the network, or written to a member-readable file.
- **Server runs are resource-limited.** `treg run --server` executes each CLI with a scrubbed environment
  (treg's own secrets removed), a per-run throwaway home, an allow-list of runnable commands, output
  redaction, and POSIX resource limits (CPU, file size, no core dumps).

## Known limitations (by design, documented on purpose)

Honesty is part of the model. Two items are deliberately deferred:

1. **Server runs do not yet have filesystem/network isolation.** The resource limits above cap denial of
   service, but a full jail (a locked-down user + egress allow-list, like the local sandbox) requires a
   container deployment and is planned. On the reference deployment there is no on-disk secret file to
   read (the encryption key is an environment variable), and only allow-listed CLIs may run.
2. **The CLI-login handshake is in-process.** The short-lived pairing state for `treg login` lives in the
   server process (it self-heals on retry and carries no rate-limit value). Running more than one server
   instance requires sticky routing for that one flow, or moving it to shared storage.

## Supported versions

This project is pre-1.0; security fixes land on `main`. Pin a commit if you need stability.
