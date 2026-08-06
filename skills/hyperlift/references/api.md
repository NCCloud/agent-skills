# Hyperlift API reference (summary)

Source of truth: **https://docs.spaceship.dev/#tag/Hyperlift**. This file is a
maintained summary; when it disagrees with the public docs, the docs win.

- **Base URL**: `https://spaceship.dev/api/v1`
- **Auth headers**: `X-API-Key` and `X-API-Secret` (both required on every call)
- **Errors**: `application/problem+json` with a `detail` field; responses also
  carry `spaceship-error-code` and `spaceship-operation-id` headers.

## Operations

| Operation | Method & path | Scope | Rate limit (per 300s) |
|---|---|---|---|
| List applications | `GET /hyperlift/applications?take=&skip=` | `hyperlift:read` | 300 / user |
| Get application | `GET /hyperlift/applications/{id}` | `hyperlift:read` | 300 / app |
| Trigger build | `POST /hyperlift/applications/{id}/build` | `hyperlift:execute` | 10 / app |
| Build logs | `GET /hyperlift/applications/{id}/build-logs?take=&cursor=` | `hyperlift:read` | 300 / app |
| Get env vars | `GET /hyperlift/applications/{id}/environment` | `hyperlift:manage` | 300 / app |
| Replace env vars | `PUT /hyperlift/applications/{id}/environment` | `hyperlift:manage` | 60 / app |
| Runtime logs | `GET /hyperlift/applications/{id}/logs?take=&cursor=` | `hyperlift:read` | 300 / app |
| Metrics | `GET /hyperlift/applications/{id}/metrics?startDate=&endDate=&interval=&metrics=` | `hyperlift:read` | 120 / app |
| Restart | `POST /hyperlift/applications/{id}/restart` | `hyperlift:execute` | 10 / app |
| Scale (start/stop) | `PUT /hyperlift/applications/{id}/scale` body `{"scale": 0|1}` | `hyperlift:execute` | 10 / app |

Note the scope asymmetry: **reading** environment variables requires
`hyperlift:manage` (not `hyperlift:read`), because values may contain secrets.

## Key parameters and constraints

- **Application id**: 1–64 chars matching `^[a-zA-Z0-9_-]+$` (typically a UUID).
- **List pagination**: `take` (1–100) and `skip` (≥0) are **required** query
  params; ordering by id is stable across pages. Response: `{items, total}`.
- **Log pagination** (`logs`, `build-logs`): `take` (1–100, default 100) and an
  opaque `cursor` (`^[A-Za-z0-9_-]{1,64}$`). Each response returns only lines
  produced *after* the cursor, so passing the newest cursor back yields no
  duplicates. Response: `{items: [{message, timestamp?}], cursor?, finished}`.
  `finished: true` means the stream ended — build completed (build logs) or
  application stopped (runtime logs). Note it is a property of the *page*, not
  the build: a partial page from an already-completed build still returns
  `finished: false` until you page through to the end of the stream, so use
  the application's `buildStatus` (not a single log page) to decide whether a
  build is done. A later build/start creates a new stream.
- **Application object**: `id`, `status` (`creating`, `instart`, `running`,
  `instop`, `stopped`, `deleting`, `failure`, `restarting`, `created`,
  `deploying`, `resetting`), `buildStatus` (`building`, `failed`, `built`,
  `none`), `plan`, `domain?`, `scale` (0|1, null while provisioning),
  `branch?`, `githubInstallationId?`, `githubRepositoryFullName?`,
  `dockerfilePath?`, `automaticBuildEnabled?`, `createdAt`, `updatedAt?`.
- **Build**: asynchronous. `POST .../build` returns `{id}` immediately; track
  progress via `buildStatus` on the application or by following build logs.
- **Environment variables**: the `PUT` is a **full replacement** — variables
  omitted from `{"items": {...}}` are removed. At most **20** variables. Names:
  ASCII letters, digits, underscores, dashes and spaces; must not start with a
  digit; ≤128 chars; normalized on write (upper-cased, dashes/spaces →
  underscores, so `db-user` is stored as `DB_USER`). Values: strings ≤16384
  chars. `APPLICATION_PORT` (default `8080`) is the port Hyperlift connects to
  the application on; an empty value unexposes the application.
- **Metrics**: all four query params are required — `startDate`/`endDate`
  (ISO-8601 UTC datetimes), `interval` (`^\d+(s|m|h|d)$`, e.g. `10m`), and
  `metrics` (comma-separated). A request may span at most **1500 intervals**.
  Documented metric names: `memoryUsageBytes`, `cpuUsagePercentage`,
  `networkReceiveRateBytes`, `networkTransmitRateBytes`,
  `ephemeralStorageUsedMebibytes`, `persistentStorageUsedMebibytes`. Response
  series carry `name`, `unit` (`bytes`, `percent`, `bytesPerSecond`,
  `mebibytes`), optional `quota` (memory and storage only), and `samples`
  (`{timestamp, value}`, value averaged over the interval bucket).
- **Scale**: `0` stops the application, `1` starts it. There is no
  multi-instance scaling through this endpoint.

## Rate-limit responses

`429` responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset`, and `Retry-After` (seconds) headers. Wait at least
`Retry-After` before retrying; never auto-retry mutating operations.
