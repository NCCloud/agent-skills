#!/usr/bin/env python3
"""Command-line client for the Spaceship Hyperlift API.

Manages Hyperlift applications through the Spaceship public API
(https://docs.spaceship.dev/#tag/Hyperlift). Uses only the Python standard
library.

Credentials are read from the environment:
    SPACESHIP_API_KEY       API key (required)
    SPACESHIP_API_SECRET    API secret (required)
    SPACESHIP_API_BASE_URL  Base URL override (default: https://spaceship.dev/api/v1)

Exit codes:
    0  success
    1  API, network, or response error
    2  invalid command-line usage
    3  confirmation required (mutating command invoked without --yes)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

DEFAULT_BASE_URL = "https://spaceship.dev/api/v1"
ENV_KEY = "SPACESHIP_API_KEY"
ENV_SECRET = "SPACESHIP_API_SECRET"
ENV_BASE_URL = "SPACESHIP_API_BASE_URL"

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "nccloud-agent-skills-hyperlift/1.0 (+https://github.com/NCCloud/agent-skills)"

# Documented API constraints (see references/api.md; source of truth is
# https://docs.spaceship.dev/#tag/Hyperlift).
MAX_ENV_VARS = 20
MAX_ENV_NAME_LENGTH = 128
MAX_ENV_VALUE_LENGTH = 16384
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_\- ][A-Za-z0-9_\- ]*$")
APP_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$")
MAX_METRIC_INTERVALS = 1500

METRIC_NAMES = [
    "memoryUsageBytes",
    "cpuUsagePercentage",
    "networkReceiveRateBytes",
    "networkTransmitRateBytes",
    "ephemeralStorageUsedMebibytes",
    "persistentStorageUsedMebibytes",
]

TERMINAL_BUILD_STATUSES = {"built", "failed"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIRM = 3

# Scope required per command, used to enrich 403 error messages.
COMMAND_SCOPES = {
    "list": "hyperlift:read",
    "get": "hyperlift:read",
    "build": "hyperlift:execute",
    "build-logs": "hyperlift:read",
    "logs": "hyperlift:read",
    "metrics": "hyperlift:read",
    "env-get": "hyperlift:manage",
    "env-set": "hyperlift:manage",
    "restart": "hyperlift:execute",
    "scale": "hyperlift:execute",
    "start": "hyperlift:execute",
    "stop": "hyperlift:execute",
}


class ApiError(Exception):
    """An error returned by the API or encountered while calling it."""

    def __init__(
        self,
        status: int,
        detail: str,
        error_code: str | None = None,
        retry_after: int | None = None,
        validation: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.error_code = error_code
        self.retry_after = retry_after
        self.validation = validation or []


class Client:
    """Minimal HTTP client for the Spaceship public API."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = DEFAULT_BASE_URL) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Perform one API request and return the decoded JSON body (or None)."""
        url = self.base_url + path
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url += "?" + urllib.parse.urlencode(filtered)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "X-API-Key": self.api_key,
            "X-API-Secret": self.api_secret,
            "Accept": "application/json",
            # Cloudflare in front of the API blocks the default Python-urllib
            # user agent, so send an explicit one.
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._to_api_error(exc) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"network error: {exc.reason}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(0, "the API returned a malformed (non-JSON) response") from None

    @staticmethod
    def _to_api_error(exc: urllib.error.HTTPError) -> ApiError:
        detail = ""
        validation: list[dict[str, str]] = []
        try:
            payload = json.loads(exc.read() or b"{}")
            detail = payload.get("detail", "")
            validation = payload.get("data") or []
        except ValueError:
            pass
        retry_after: int | None = None
        raw_retry = exc.headers.get("Retry-After")
        if raw_retry and raw_retry.isdigit():
            retry_after = int(raw_retry)
        return ApiError(
            status=exc.code,
            detail=detail or exc.reason or f"HTTP {exc.code}",
            error_code=exc.headers.get("spaceship-error-code"),
            retry_after=retry_after,
            validation=validation,
        )


# ---------------------------------------------------------------------------
# Environment-variable helpers
# ---------------------------------------------------------------------------


def normalize_env_name(name: str) -> str:
    """Normalize a variable name the way the API does on write.

    Upper-cased; dashes and spaces become underscores (`db-user` -> `DB_USER`).
    """
    return name.upper().replace("-", "_").replace(" ", "_")


def validate_env_name(name: str) -> None:
    """Raise ValueError when a variable name violates the documented rules."""
    if not name:
        raise ValueError("environment variable name must not be empty")
    if len(name) > MAX_ENV_NAME_LENGTH:
        raise ValueError(
            f"environment variable name {name[:40]!r}... exceeds {MAX_ENV_NAME_LENGTH} characters"
        )
    if not ENV_NAME_PATTERN.match(name):
        raise ValueError(
            f"invalid environment variable name {name!r}: only ASCII letters, digits, "
            "underscores, dashes and spaces are allowed, and it must not start with a digit"
        )


def merge_environment(
    current: dict[str, str],
    sets: dict[str, str],
    removes: list[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Merge requested changes into the current variable set.

    The Hyperlift environment update is a full replacement, so the merged
    result must carry every variable that should survive. Names are normalized
    before comparison to mirror the API's write behavior.

    Returns the merged map and a plan of {added, updated, removed, kept} names.
    """
    merged = {normalize_env_name(k): v for k, v in current.items()}
    plan: dict[str, list[str]] = {"added": [], "updated": [], "removed": [], "kept": []}

    for raw_name, value in sets.items():
        validate_env_name(raw_name)
        name = normalize_env_name(raw_name)
        if name in merged:
            plan["updated"].append(name)
        else:
            plan["added"].append(name)
        merged[name] = value

    for raw_name in removes:
        name = normalize_env_name(raw_name)
        if name in merged:
            del merged[name]
            plan["removed"].append(name)

    touched = set(plan["added"]) | set(plan["updated"])
    plan["kept"] = sorted(name for name in merged if name not in touched)
    return merged, plan


def validate_environment(items: dict[str, str]) -> None:
    """Raise ValueError when the merged set violates documented constraints.

    Error messages reference variable names only, never values.
    """
    if len(items) > MAX_ENV_VARS:
        raise ValueError(
            f"the merged set has {len(items)} variables; Hyperlift allows at most {MAX_ENV_VARS}"
        )
    for name, value in items.items():
        validate_env_name(name)
        if not isinstance(value, str):
            raise ValueError(f"value of {name} must be a string")
        if len(value) > MAX_ENV_VALUE_LENGTH:
            raise ValueError(
                f"value of {name} exceeds the maximum length of {MAX_ENV_VALUE_LENGTH} characters"
            )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


# Sensitive strings (credentials, environment-variable values seen during a
# command) that must never appear in output. Populated at runtime.
SENSITIVE_VALUES: list[str] = []


def redact(text: str, secrets: list[str]) -> str:
    """Scrub credential material and control characters from output text."""
    for secret in list(secrets) + SENSITIVE_VALUES:
        if secret:
            text = text.replace(secret, "***")
    return strip_ansi(text)


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False))


# CSI sequences (colors, cursor movement), OSC sequences (window titles,
# hyperlinks), other single-character escapes, and raw C0 control characters
# except tab/newline. Log lines are untrusted input; anything that could smuggle
# terminal control past the agent gets stripped.
ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC ... BEL or ST
    r"|\x1b[@-_]"  # other C1 escapes
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # raw control chars (keep \t, \n)
)


def strip_ansi(text: str) -> str:
    """Remove ANSI/terminal escape sequences and control characters."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def clean_log_page(page: dict[str, Any]) -> dict[str, Any]:
    """Return a log page with ANSI escapes stripped from every message."""
    return {
        **page,
        "items": [
            {**line, "message": strip_ansi(line.get("message", ""))}
            for line in page.get("items", [])
        ],
    }


def format_log_line(line: dict[str, Any]) -> str:
    timestamp = line.get("timestamp")
    message = strip_ansi(line.get("message", ""))
    return f"{timestamp}  {message}" if timestamp else message


def format_application_text(app: dict[str, Any]) -> str:
    rows = [
        ("id", app.get("id")),
        ("status", app.get("status")),
        ("buildStatus", app.get("buildStatus")),
        ("plan", app.get("plan")),
        ("scale", app.get("scale")),
        ("domain", app.get("domain")),
        ("repository", app.get("githubRepositoryFullName")),
        ("branch", app.get("branch")),
        ("dockerfilePath", app.get("dockerfilePath")),
        ("automaticBuildEnabled", app.get("automaticBuildEnabled")),
        ("createdAt", app.get("createdAt")),
        ("updatedAt", app.get("updatedAt")),
    ]
    return "\n".join(f"{key:>22}: {value}" for key, value in rows)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def app_path(app_id: str, suffix: str = "") -> str:
    return f"/hyperlift/applications/{app_id}{suffix}"


def cmd_list(client: Client, args: argparse.Namespace) -> int:
    if args.all:
        items: list[dict[str, Any]] = []
        skip = 0
        while True:
            page = client.request(
                "GET", "/hyperlift/applications", query={"take": 100, "skip": skip}
            )
            items.extend(page.get("items", []))
            total = page.get("total", len(items))
            if len(items) >= total or not page.get("items"):
                break
            skip += 100
        result = {"items": items, "total": len(items)}
    else:
        result = client.request(
            "GET", "/hyperlift/applications", query={"take": args.take, "skip": args.skip}
        )
    if args.output == "text":
        for app in result.get("items", []):
            print(
                f"{app.get('id')}  status={app.get('status')}  "
                f"build={app.get('buildStatus')}  plan={app.get('plan')}  "
                f"domain={app.get('domain')}"
            )
        print(f"total: {result.get('total')}")
    else:
        emit_json(result)
    return EXIT_OK


def cmd_get(client: Client, args: argparse.Namespace) -> int:
    app = client.request("GET", app_path(args.app_id))
    if args.output == "text":
        print(format_application_text(app))
    else:
        emit_json(app)
    return EXIT_OK


def cmd_build(client: Client, args: argparse.Namespace) -> int:
    require_yes(args, f"trigger a build of application {args.app_id}")
    result = client.request("POST", app_path(args.app_id, "/build"))
    print(
        f"Build triggered for application {result.get('id', args.app_id)}. "
        "The build runs asynchronously; poll buildStatus with 'get' or follow 'build-logs'.",
        file=sys.stderr,
    )
    if args.watch:
        return watch_build(client, args)
    emit_json(result)
    return EXIT_OK


def watch_build(client: Client, args: argparse.Namespace) -> int:
    """Follow build logs until the build reaches a terminal state or times out."""
    status = follow_logs(
        client,
        app_path(args.app_id, "/build-logs"),
        take=100,
        cursor=None,
        timeout=args.timeout,
        interval=args.interval,
    )
    app = client.request("GET", app_path(args.app_id))
    build_status = app.get("buildStatus")
    print(f"buildStatus: {build_status}", file=sys.stderr)
    if status == "timeout":
        print(
            f"Timed out after {args.timeout}s waiting for the build to finish. "
            "The build may still be running; check again with 'get' or 'build-logs'.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK if build_status != "failed" else EXIT_ERROR


def follow_logs(
    client: Client,
    path: str,
    take: int,
    cursor: str | None,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Poll a log endpoint, printing new lines until finished or timeout.

    The API's cursor already guarantees no duplicate lines: each response
    returns only lines produced after the cursor passed in.
    """
    deadline = time.monotonic() + timeout
    while True:
        page = client.request("GET", path, query={"take": take, "cursor": cursor})
        for line in page.get("items", []):
            print(format_log_line(line))
        cursor = page.get("cursor") or cursor
        if page.get("finished"):
            return "finished"
        if time.monotonic() >= deadline:
            return "timeout"
        sleep(interval)


def cmd_page_or_follow_logs(client: Client, args: argparse.Namespace, suffix: str) -> int:
    path = app_path(args.app_id, suffix)
    if args.follow:
        status = follow_logs(
            client,
            path,
            take=args.take,
            cursor=args.cursor,
            timeout=args.timeout,
            interval=args.interval,
        )
        if status == "timeout":
            print(
                f"Stopped following after {args.timeout}s; the log stream has not finished.",
                file=sys.stderr,
            )
        return EXIT_OK
    page = client.request("GET", path, query={"take": args.take, "cursor": args.cursor})
    if args.output == "text":
        for line in page.get("items", []):
            print(format_log_line(line))
        print(
            f"finished: {page.get('finished')}  cursor: {page.get('cursor')}",
            file=sys.stderr,
        )
    else:
        emit_json(clean_log_page(page))
    return EXIT_OK


def cmd_build_logs(client: Client, args: argparse.Namespace) -> int:
    return cmd_page_or_follow_logs(client, args, "/build-logs")


def cmd_logs(client: Client, args: argparse.Namespace) -> int:
    return cmd_page_or_follow_logs(client, args, "/logs")


def cmd_env_get(client: Client, args: argparse.Namespace) -> int:
    result = client.request("GET", app_path(args.app_id, "/environment"))
    items = result.get("items", {})
    if not args.show_values:
        SENSITIVE_VALUES.extend(v for v in items.values() if v)
    if not args.show_values:
        items = {name: "***" for name in items}
    payload = {"id": result.get("id"), "items": items}
    if args.output == "text":
        for name, value in sorted(payload["items"].items()):
            print(f"{name}={value}")
    else:
        emit_json(payload)
    if not args.show_values:
        print(
            f"{len(items)} variable(s); values hidden (use --show-values to include them).",
            file=sys.stderr,
        )
    return EXIT_OK


def parse_set_option(option: str) -> tuple[str, str]:
    """Parse a NAME=VALUE option; the value may be empty."""
    if "=" not in option:
        raise ValueError(f"--set expects NAME=VALUE, got {option.split('=', 1)[0]!r}")
    name, value = option.split("=", 1)
    return name, value


def cmd_env_set(client: Client, args: argparse.Namespace) -> int:
    sets: dict[str, str] = {}
    for option in args.set or []:
        name, value = parse_set_option(option)
        sets[name] = value
    removes = list(args.remove or [])
    if not sets and not removes:
        print("Nothing to do: pass at least one --set NAME=VALUE or --remove NAME.", file=sys.stderr)
        return EXIT_USAGE

    current = client.request("GET", app_path(args.app_id, "/environment")).get("items", {})
    merged, plan = merge_environment(current, sets, removes)
    # Anything we hold or send may be echoed back by a failing API call;
    # register every value so error output can never leak one.
    SENSITIVE_VALUES.extend(v for v in {**current, **merged}.values() if v)
    validate_environment(merged)

    # The plan is names-only by design: values must never be printed.
    print(f"Environment update plan for application {args.app_id}:", file=sys.stderr)
    for action in ("added", "updated", "removed", "kept"):
        names = plan[action]
        print(f"  {action} ({len(names)}): {', '.join(sorted(names)) or '-'}", file=sys.stderr)
    print(
        f"  final count: {len(merged)}/{MAX_ENV_VARS} "
        "(full replacement: all 'kept' variables are re-sent unchanged)",
        file=sys.stderr,
    )

    require_yes(args, f"replace the environment variables of application {args.app_id}")
    result = client.request("PUT", app_path(args.app_id, "/environment"), body={"items": merged})
    emit_json({"id": result.get("id", args.app_id), "variables": sorted(merged), "plan": plan})
    return EXIT_OK


def cmd_metrics(client: Client, args: argparse.Namespace) -> int:
    start, end = resolve_metrics_range(args)
    validate_metrics_query(start, end, args.interval, args.metrics)
    query = {
        "startDate": format_iso(start),
        "endDate": format_iso(end),
        "interval": args.interval,
        "metrics": args.metrics,
    }
    result = client.request("GET", app_path(args.app_id, "/metrics"), query=query)
    if args.output == "text":
        print(f"range: {query['startDate']} .. {query['endDate']}  interval: {args.interval}")
        for series in result.get("metrics", []):
            samples = series.get("samples", [])
            latest = samples[-1] if samples else None
            quota = series.get("quota")
            line = f"{series.get('name'):>34} [{series.get('unit')}]"
            if latest:
                line += f"  latest={latest['value']} @ {latest['timestamp']}"
            else:
                line += "  no samples"
            if quota is not None:
                line += f"  quota={quota}"
            print(line)
    else:
        emit_json(result)
    return EXIT_OK


def resolve_metrics_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.start and args.end:
        return parse_iso(args.start), parse_iso(args.end)
    if args.start or args.end:
        raise ValueError("--start and --end must be provided together (or use --last)")
    end = datetime.now(timezone.utc)
    return end - parse_duration(args.last), end


def parse_duration(value: str) -> timedelta:
    match = INTERVAL_PATTERN.match(value)
    if not match:
        raise ValueError(f"invalid duration {value!r}: expected a number plus s, m, h or d (e.g. 1h)")
    amount = int(match.group(1))
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return timedelta(**{unit: amount})


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def validate_metrics_query(start: datetime, end: datetime, interval: str, metrics: str) -> None:
    if end <= start:
        raise ValueError("the metrics range end must be after its start")
    span = (end - start).total_seconds()
    bucket = parse_duration(interval).total_seconds()
    if bucket <= 0:
        raise ValueError("the metrics interval must be positive")
    if span / bucket > MAX_METRIC_INTERVALS:
        raise ValueError(
            f"the requested range spans {int(span / bucket)} intervals; the API allows at most "
            f"{MAX_METRIC_INTERVALS}. Use a coarser --interval or a shorter range."
        )
    for name in metrics.split(","):
        if name not in METRIC_NAMES:
            raise ValueError(
                f"unknown metric {name!r}; documented metrics: {', '.join(METRIC_NAMES)}"
            )


def cmd_restart(client: Client, args: argparse.Namespace) -> int:
    require_yes(args, f"restart application {args.app_id}")
    result = client.request("POST", app_path(args.app_id, "/restart"))
    emit_json(result)
    return EXIT_OK


def cmd_scale(client: Client, args: argparse.Namespace) -> int:
    action = "stop" if args.scale == 0 else "start"
    require_yes(args, f"{action} application {args.app_id} (scale={args.scale})")
    result = client.request("PUT", app_path(args.app_id, "/scale"), body={"scale": args.scale})
    emit_json(result)
    return EXIT_OK


def require_yes(args: argparse.Namespace, action: str) -> None:
    if not args.yes:
        print(
            f"Refusing to {action}: this is a mutating operation. "
            "Re-run with --yes after obtaining explicit user confirmation.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIRM)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def validated_app_id(value: str) -> str:
    if not APP_ID_PATTERN.match(value):
        raise argparse.ArgumentTypeError(
            "application id must be 1-64 characters of letters, digits, '_' or '-'"
        )
    return value


def validated_cursor(value: str) -> str:
    if not CURSOR_PATTERN.match(value):
        raise argparse.ArgumentTypeError("cursor must be 1-64 characters of [A-Za-z0-9_-]")
    return value


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"value must be between {low} and {high}")
        return number

    return parse


def add_output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "-o",
        choices=("json", "text"),
        default="json",
        help="output format (default: json)",
    )


def add_follow_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--take", type=bounded_int(1, 100), default=100, help="lines per page (1-100)")
    parser.add_argument("--cursor", type=validated_cursor, help="resume cursor from a previous page")
    parser.add_argument("--follow", action="store_true", help="poll for new lines until the stream finishes")
    parser.add_argument("--timeout", type=float, default=300, help="max seconds to follow (default 300)")
    parser.add_argument("--interval", type=float, default=3, help="seconds between polls (default 3)")


def add_yes_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm this mutating operation (agents: obtain explicit user confirmation first)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyperlift",
        description="Manage Hyperlift applications through the Spaceship public API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list Hyperlift applications")
    p.add_argument("--take", type=bounded_int(1, 100), default=100, help="items per page (1-100)")
    p.add_argument("--skip", type=bounded_int(0, 2**31 - 1), default=0, help="items to skip")
    p.add_argument("--all", action="store_true", help="fetch every page")
    add_output_option(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="get one application")
    p.add_argument("app_id", type=validated_app_id, help="application identifier")
    add_output_option(p)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("build", help="trigger a build (mutating)")
    p.add_argument("app_id", type=validated_app_id)
    p.add_argument("--watch", action="store_true", help="follow build logs until the build finishes")
    p.add_argument("--timeout", type=float, default=900, help="max seconds to watch (default 900)")
    p.add_argument("--interval", type=float, default=3, help="seconds between polls (default 3)")
    add_yes_option(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("build-logs", help="read build logs")
    p.add_argument("app_id", type=validated_app_id)
    add_follow_options(p)
    add_output_option(p)
    p.set_defaults(func=cmd_build_logs)

    p = sub.add_parser("logs", help="read runtime logs")
    p.add_argument("app_id", type=validated_app_id)
    add_follow_options(p)
    add_output_option(p)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("env-get", help="read environment variables (values hidden by default)")
    p.add_argument("app_id", type=validated_app_id)
    p.add_argument("--show-values", action="store_true", help="include variable values in the output")
    add_output_option(p)
    p.set_defaults(func=cmd_env_get)

    p = sub.add_parser(
        "env-set",
        help="add/update/remove environment variables (mutating; merges, then replaces the full set)",
    )
    p.add_argument("app_id", type=validated_app_id)
    p.add_argument("--set", action="append", metavar="NAME=VALUE", help="set a variable (repeatable)")
    p.add_argument("--remove", action="append", metavar="NAME", help="remove a variable (repeatable)")
    add_yes_option(p)
    p.set_defaults(func=cmd_env_set)

    p = sub.add_parser("metrics", help="read resource metrics")
    p.add_argument("app_id", type=validated_app_id)
    p.add_argument("--start", help="range start, ISO-8601 UTC (with --end)")
    p.add_argument("--end", help="range end, ISO-8601 UTC (with --start)")
    p.add_argument("--last", default="1h", help="relative range ending now, e.g. 30m, 6h, 1d (default 1h)")
    p.add_argument("--interval", default="1m", help="bucket size, e.g. 10s, 1m, 1h (default 1m)")
    p.add_argument("--metrics", default=",".join(METRIC_NAMES), help="comma-separated metric names")
    add_output_option(p)
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("restart", help="restart an application (mutating)")
    p.add_argument("app_id", type=validated_app_id)
    add_yes_option(p)
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("scale", help="set application scale: 0 stops, 1 starts (mutating)")
    p.add_argument("app_id", type=validated_app_id)
    p.add_argument("--scale", type=int, choices=(0, 1), required=True)
    add_yes_option(p)
    p.set_defaults(func=cmd_scale)

    p = sub.add_parser("start", help="start an application (scale=1; mutating)")
    p.add_argument("app_id", type=validated_app_id)
    add_yes_option(p)
    p.set_defaults(func=cmd_scale, scale=1)

    p = sub.add_parser("stop", help="stop an application (scale=0; mutating)")
    p.add_argument("app_id", type=validated_app_id)
    add_yes_option(p)
    p.set_defaults(func=cmd_scale, scale=0)

    return parser


def describe_api_error(error: ApiError, command: str) -> str:
    scope = COMMAND_SCOPES.get(command)
    parts = [f"API error (HTTP {error.status})" if error.status else "request failed"]
    if error.detail:
        parts.append(error.detail)
    if error.error_code:
        parts.append(f"[{error.error_code}]")
    for item in error.validation:
        parts.append(f"{item.get('field')}: {item.get('details')}")
    if error.status == 401:
        parts.append(
            "Check that SPACESHIP_API_KEY and SPACESHIP_API_SECRET are set to a valid, "
            "enabled Spaceship API key pair."
        )
    elif error.status == 403 and scope:
        parts.append(f"This operation requires the '{scope}' scope on the API key.")
    elif error.status == 429:
        if error.retry_after is not None:
            parts.append(f"Rate limited; retry after {error.retry_after}s.")
        else:
            parts.append("Rate limited; wait before retrying.")
        parts.append("Do not automatically retry mutating operations.")
    elif error.status == 404:
        parts.append("Check the application id with the 'list' command.")
    return " ".join(parts)


def load_dotenv_credentials() -> dict[str, str]:
    """Read Spaceship credentials from a .env file when absent from the environment.

    Searches the working directory and its parents for a `.env` file and picks
    up only the credential keys, without overriding existing environment
    variables. This lets a user drop credentials into their project's .env and
    use the skill with no further setup.

    The base-URL override is deliberately NOT read from .env: a malicious
    repository could commit a .env pointing the base URL at an attacker host,
    which would receive the real credentials from the user's environment.
    """
    wanted = {ENV_KEY, ENV_SECRET}
    found: dict[str, str] = {}
    directory = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(directory, ".env")
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        if line.startswith("export "):
                            line = line[len("export ") :]
                        name, value = line.split("=", 1)
                        name = name.strip()
                        if name in wanted and name not in os.environ and name not in found:
                            found[name] = value.strip().strip("'\"")
            except OSError:
                pass
            break  # use the nearest .env only
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return found


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dotenv = load_dotenv_credentials()
    api_key = os.environ.get(ENV_KEY) or dotenv.get(ENV_KEY, "")
    api_secret = os.environ.get(ENV_SECRET) or dotenv.get(ENV_SECRET, "")
    base_url = os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    if base_url != DEFAULT_BASE_URL:
        print(f"note: using non-default API base URL {base_url}", file=sys.stderr)
    secrets = [api_key, api_secret]
    if not api_key or not api_secret:
        print(
            f"error: {ENV_KEY} and {ENV_SECRET} are not set. Ask the user to create an "
            "API key in the Spaceship API manager (spaceship.com) with the hyperlift "
            "scopes they need, then put both values in the project's .env file or "
            "export them in their shell. Do NOT ask the user to paste the secret "
            "into the chat.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    client = Client(api_key, api_secret, base_url)
    try:
        return args.func(client, args)
    except ApiError as exc:
        print("error: " + redact(describe_api_error(exc, args.command), secrets), file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print("error: " + redact(str(exc), secrets), file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
