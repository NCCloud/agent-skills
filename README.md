# Namecheap Cloud Agent Skills

Standalone [Agent Skills](https://agentskills.io) for managing Namecheap Cloud
products.

Currently this repository provides one skill — **Hyperlift**. Skills for other
Namecheap Cloud products (EasyWP, Supersonic CDN) may be added later under the
same layout.

```
skills/
└── hyperlift/
    ├── SKILL.md            # skill definition (name + description frontmatter)
    ├── scripts/hyperlift.py  # stdlib-only Python CLI for the Hyperlift API
    ├── references/         # API summary, workflows, troubleshooting
    └── tests/              # unit tests (mocked HTTP, never hit the real API)
```

## Hyperlift skill

Lets a coding agent manage and troubleshoot Hyperlift applications:

- List applications and inspect one (status, build status, scale, domain, repo)
- Trigger builds and monitor them to completion (`built` / `failed`)
- Read build logs and runtime logs, with cursor-based following
- Inspect resource metrics (memory, CPU, network, storage) against quotas
- Read and safely update environment variables — changes merge into the
  existing set (nothing is silently deleted) and secret values are never
  displayed
- Restart, start, and stop applications

### Credentials

Create an API key in the [Spaceship API manager](https://www.spaceship.com)
with the scopes you need: `hyperlift:read` (inspect/logs/metrics),
`hyperlift:execute` (build/restart/start/stop), `hyperlift:manage` (read and
write environment variables).

The API authenticates with a **key/secret pair**: `SPACESHIP_API_KEY`
identifies the credential and `SPACESHIP_API_SECRET` proves it. **Both are
required on every request** — either one alone is rejected with HTTP 401.

Where to put the key and secret depends on how you installed the skill —
"your project" always means **the repository you run your agent in** (your
app's codebase), never this repository:

| Install | Where credentials go |
|---|---|
| Per-project | Your project's `.env` file (git-ignored). The CLI finds it automatically — it searches the working directory and its parents. |
| Global (`-g`) | Your shell profile, so it works from any directory: |

```bash
# ~/.zshenv (or ~/.bashrc)
export SPACESHIP_API_KEY=...
export SPACESHIP_API_SECRET=...
```

Environment variables always win over `.env`. Optionally set
`SPACESHIP_API_BASE_URL` to override the default
`https://spaceship.dev/api/v1`. Never paste the secret into an agent chat —
put it in one of the places above yourself; if it does end up in a chat,
rotate the key.

You don't need any credentials just to install the skill: the first command
that needs them fails with instructions, your agent walks you through this
setup once, and every later use works without prompts.

## Installation

Run inside the project where you'll use the skill, with the
[skills](https://github.com/vercel-labs/skills) installer:

```bash
npx skills add NCCloud/agent-skills --skill hyperlift
```

Or install once for all projects (global):

```bash
npx skills add NCCloud/agent-skills --skill hyperlift -g
```

Or for a specific agent, e.g. Claude Code — copy the skill into your project:

```bash
mkdir -p .claude/skills && cp -r skills/hyperlift .claude/skills/hyperlift
```

## Invocation

Ask your agent, for example:

- "Use the Hyperlift skill to list my applications."
- "Use the Hyperlift skill to investigate why this application build failed."
- "Use the Hyperlift skill to show the latest runtime logs."
- "Use the Hyperlift skill to add an environment variable without removing the
  existing variables."
- "Use the Hyperlift skill to stop an application."

