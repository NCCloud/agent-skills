# Hyperlift agent workflows

Step-by-step procedures for common tasks. All commands are
`python3 scripts/hyperlift.py ...` run from the skill directory. Mutating steps
are marked **[confirm]** — run them with `--yes` when they match the user's
intent; confirm with the user first when intent is unclear or the impact is
high (see the confirmation guidance in SKILL.md).

## Select an application

1. If the user gave an id, validate it with `get <app-id>`.
2. Otherwise run `list --all` and match on domain, repository, or branch.
3. One match → proceed. Several → show `id / domain / repository / status` and
   ask the user to choose. Never guess between candidates.

## Inspect application status

1. `get <app-id>` — read `status`, `buildStatus`, `scale`.
2. Interpret: `running` + `built` is healthy; `failure` or `buildStatus:
   failed` needs logs; `stopped` with `scale: 0` was stopped deliberately;
   transitional states (`instart`, `instop`, `deploying`, `restarting`,
   `resetting`, `creating`) usually resolve on their own — re-check after a
   short wait.

## Trigger and monitor a build

1. Optionally `get <app-id>` first; if `buildStatus` is already `building`,
   ask the user whether they really want another build.
2. **[confirm]** `build <app-id> --yes --watch` — streams build logs until
   `finished: true`, then reports the final `buildStatus`.
3. Report `built` or `failed` explicitly. On `--watch` timeout, say the build
   is still running and check later with `get`.

## Diagnose a failed build

1. `get <app-id>` — confirm `buildStatus: failed`.
2. `build-logs <app-id>` — read the last page; page back is not possible, so
   fetch without a cursor to start from the beginning of the stream and follow
   the cursor forward if needed.
3. Look for the first error (missing dependency, Dockerfile error, wrong
   `dockerfilePath`, test failure). Check `dockerfilePath` and `branch` on the
   application object against what the user expects.
4. Propose a fix. Only rebuild after the underlying cause is addressed
   (**[confirm]** for the rebuild).

## Inspect runtime problems

1. `get <app-id>` — is it `running`? What does `scale` say?
2. `logs <app-id>` (add `--follow` to watch live) — look for crashes, port
   binding errors, unhandled exceptions.
3. `metrics <app-id> --last 1h --interval 1m -o text` — compare memory usage
   against its quota; sustained CPU near 100% or memory at quota suggests the
   plan is undersized or the app is leaking. Present this as an inference from
   the numbers, not a diagnosis.
4. If the app listens on a port other than 8080, check `APPLICATION_PORT`
   (`env-get <app-id>` — names only is enough to see whether it is set).

## Safely update environment variables

1. `env-get <app-id>` — see which variables exist (names only).
2. Dry-run: `env-set <app-id> --set NAME=VALUE --remove OLD_NAME` (no `--yes`)
   — prints the added/updated/removed/kept plan and exits with code 3.
3. Show the user the plan. Warn them that applying it **restarts the
   application** (observed behavior: the app redeploys with the new
   environment immediately after the update).
4. **[confirm]** Re-run the same command with `--yes`.
5. Verify with `env-get <app-id>` that the expected names are present.

## Restart an application

1. `get <app-id>` — it should be `running` (restarting a stopped app: use
   `start` instead).
2. **[confirm]** `restart <app-id> --yes`.
3. Poll `get <app-id>` until `status` returns to `running`; then check `logs`
   for a clean startup.

## Start or stop an application

1. `get <app-id>` — check the current `scale`/`status`; skip the call if it is
   already in the desired state.
2. **[confirm]** `start <app-id> --yes` or `stop <app-id> --yes`.
3. Poll `get <app-id>` until `running` (or `stopped`). For stop, note that
   runtime logs end (`finished: true`) once the app is down.
