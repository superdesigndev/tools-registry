# @superdesign/treg

npm launcher for **treg** — call your team's APIs through one credential-injecting
proxy, with no keys on your machine.

```bash
npx @superdesign/treg login
npx @superdesign/treg tool ls
```

Or install globally:

```bash
npm install -g @superdesign/treg
treg login
```

treg itself is a Python CLI; this package finds it on your machine and runs it,
installing it first (via `uv`, `pipx`, or `pip3`) if it's missing.

- Docs & interactive tutorial: https://treg.superdesign.dev/tutorial
- Source: https://github.com/superdesigndev/tools-registry
