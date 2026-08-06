# Hyperlift troubleshooting

Common problems, what they look like, and what to do. Error responses carry a
`detail` message plus `spaceship-error-code` / `spaceship-operation-id`
headers — include the operation id when escalating to Spaceship support.

## Authentication and permissions

| Symptom | Cause | Fix |
|---|---|---|
| CLI exits before calling the API, "must be set in the environment" | `SPACESHIP_API_KEY` / `SPACESHIP_API_SECRET` missing | Create an API key in the Spaceship API manager and export both values. |
| HTTP 401 | Wrong, disabled, or revoked key/secret pair | Re-check both values; regenerate the key if needed. |
| HTTP 403 on list/get/logs/metrics | Key lacks `hyperlift:read` | Recreate or edit the key with the scope. |
| HTTP 403 on build/restart/start/stop/scale | Key lacks `hyperlift:execute` | Same. |
| HTTP 403 on `env-get` or `env-set` | Key lacks `hyperlift:manage` — note that even *reading* env vars needs it | Same. |

## Builds

- **`buildStatus: failed`** — read `build-logs` from the start of the stream;
  the first error is usually the real one. Frequent causes: wrong
  `dockerfilePath`, broken Dockerfile step, missing dependency, pushing to a
  branch other than the connected `branch`.
- **Build seems stuck in `building`** — keep following `build-logs`; only the
  stream's `finished: true` (or a `buildStatus` change) is authoritative. Rate
  limit for triggering builds is 10 per 300s per app — don't re-trigger to
  "unstick" it.
- **HTTP 422 on build** — the application may be in a state that cannot build
  (e.g. still `creating`); check `get` first.

## Runtime

- **App `running` but unreachable** — check `APPLICATION_PORT`: Hyperlift
  routes to port 8080 by default; if the app listens elsewhere, set
  `APPLICATION_PORT` accordingly. An **empty** `APPLICATION_PORT` deliberately
  unexposes the app.
- **Crash loop** — `logs --follow` while the app restarts; look at the last
  lines before each `finished: true`/restart boundary.
- **`status: failure`** — inspect runtime logs, then try `restart` (with user
  confirmation). If it persists, the build output itself may be broken —
  rebuild after fixing.
- **Out-of-memory suspicion** — `metrics --last 6h --interval 5m`: compare
  `memoryUsageBytes` against its `quota`. Values repeatedly hitting the quota
  suggest (but do not prove) memory exhaustion.

## Environment variables

- **A variable disappeared after an update** — the PUT is a full replacement;
  something updated the set without carrying that variable. Restore it with
  `env-set --set NAME=VALUE` (the CLI's merge preserves everything else).
- **HTTP 400/422 on update** — check the constraints: max 20 variables, names
  ≤128 chars not starting with a digit, values ≤16384 chars. The CLI validates
  these before sending; if the API still rejects, read the validation `data`
  items in the error output (field + details).
- **"My variable's name changed"** — names are normalized on write:
  upper-cased, dashes/spaces → underscores (`db-user` → `DB_USER`). The app
  sees the normalized name.

## Rate limits (HTTP 429)

Per-300s limits: 300 list/user, 300 get/logs/build-logs/env-read per app,
120 metrics, 60 env-update, 10 build/restart/scale. On 429, read `Retry-After`
(seconds) and wait at least that long. Never automatically retry a mutating
operation — confirm with the user instead.

## Network and server errors

- **`network error: ...`** — DNS/connectivity/TLS problem on the local side, or
  a wrong `SPACESHIP_API_BASE_URL`. The default base URL is
  `https://spaceship.dev/api/v1`.
- **HTTP 500** — infrastructure error on Spaceship's side. Safe to retry
  *read* operations after a pause. For a failed mutating call, check actual
  state first (`get`, `env-get`) — the operation may or may not have applied.
- **Malformed response** — usually a proxy or captive portal in the path;
  verify the base URL and network.
