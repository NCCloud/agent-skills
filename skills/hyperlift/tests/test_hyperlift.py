"""Unit tests for the Hyperlift CLI. All HTTP traffic is mocked."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import hyperlift  # noqa: E402

KEY = "test-key"
SECRET = "test-secret-value"
APP = "3f8b9a2e-5c41-4d6a-9f0e-7b2c8d1a4e5f"


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, payload=None, headers=None):
    msg = Message()
    for name, value in (headers or {}).items():
        msg[name] = value
    body = json.dumps(payload or {"detail": f"error {status}"}).encode()
    return urllib.error.HTTPError("https://example", status, "err", msg, io.BytesIO(body))


def run_main(argv, responses, env=None):
    """Run main() with urlopen mocked; returns (exit_code, stdout, stderr, requests)."""
    requests = []
    queue = list(responses)

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    env = {hyperlift.ENV_KEY: KEY, hyperlift.ENV_SECRET: SECRET, **(env or {})}
    hyperlift.SENSITIVE_VALUES.clear()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch(
        "urllib.request.urlopen", side_effect=fake_urlopen
    ), redirect_stdout(out), redirect_stderr(err):
        try:
            code = hyperlift.main(argv)
        except SystemExit as exc:  # require_yes and argparse call sys.exit
            code = exc.code
    return code, out.getvalue(), err.getvalue(), requests


APP_PAYLOAD = {
    "id": APP,
    "status": "running",
    "buildStatus": "built",
    "plan": "starter",
    "domain": None,
    "scale": 1,
    "createdAt": "2026-01-01T00:00:00Z",
}


class AuthAndTransportTests(unittest.TestCase):
    def test_auth_headers_sent(self):
        code, _, _, requests = run_main(["get", APP], [APP_PAYLOAD])
        self.assertEqual(code, 0)
        req = requests[0]
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertEqual(req.get_header("X-api-secret"), SECRET)
        self.assertEqual(req.get_method(), "GET")

    def test_missing_credentials(self):
        import tempfile

        out, err = io.StringIO(), io.StringIO()
        old_cwd = os.getcwd()
        # Run from an isolated directory so a developer's real .env (found via
        # the parent-directory fallback) cannot leak into this test.
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                    "hyperlift.load_dotenv_credentials", return_value={}
                ), redirect_stdout(out), redirect_stderr(err):
                    code = hyperlift.main(["list"])
            finally:
                os.chdir(old_cwd)
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("SPACESHIP_API_KEY", err.getvalue())

    def test_base_url_override(self):
        code, _, _, requests = run_main(
            ["get", APP], [APP_PAYLOAD], env={hyperlift.ENV_BASE_URL: "https://alt.example/api/v1/"}
        )
        self.assertEqual(code, 0)
        self.assertTrue(requests[0].full_url.startswith(f"https://alt.example/api/v1/hyperlift/applications/{APP}"))

    def test_malformed_response(self):
        class BadResponse(FakeResponse):
            def read(self):
                return b"<html>not json</html>"

        def fake_urlopen(req, timeout=None):
            return BadResponse(None)

        env = {hyperlift.ENV_KEY: KEY, hyperlift.ENV_SECRET: SECRET}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ), redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = hyperlift.main(["get", APP])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("malformed", err.getvalue())


class DotenvFallbackTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_credentials_loaded_from_dotenv(self):
        with open(".env", "w") as handle:
            handle.write(f"# comment\nexport {hyperlift.ENV_KEY}='{KEY}'\n{hyperlift.ENV_SECRET}={SECRET}\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            found = hyperlift.load_dotenv_credentials()
        self.assertEqual(found[hyperlift.ENV_KEY], KEY)
        self.assertEqual(found[hyperlift.ENV_SECRET], SECRET)

    def test_dotenv_found_in_parent_directory(self):
        with open(".env", "w") as handle:
            handle.write(f"{hyperlift.ENV_KEY}={KEY}\n")
        os.makedirs("child/grandchild")
        os.chdir("child/grandchild")
        with mock.patch.dict(os.environ, {}, clear=True):
            found = hyperlift.load_dotenv_credentials()
        self.assertEqual(found[hyperlift.ENV_KEY], KEY)

    def test_environment_takes_precedence_over_dotenv(self):
        with open(".env", "w") as handle:
            handle.write(f"{hyperlift.ENV_KEY}=from-dotenv\n")
        with mock.patch.dict(os.environ, {hyperlift.ENV_KEY: "from-env"}, clear=True):
            found = hyperlift.load_dotenv_credentials()
        self.assertNotIn(hyperlift.ENV_KEY, found)

    def test_only_credential_keys_loaded(self):
        with open(".env", "w") as handle:
            handle.write(f"OTHER_SECRET=nope\n{hyperlift.ENV_KEY}={KEY}\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            found = hyperlift.load_dotenv_credentials()
        self.assertEqual(set(found), {hyperlift.ENV_KEY})

    def test_base_url_never_loaded_from_dotenv(self):
        # A checked-in .env must not be able to redirect credentials to an
        # attacker-controlled host.
        with open(".env", "w") as handle:
            handle.write(
                f"{hyperlift.ENV_BASE_URL}=https://evil.example/api/v1\n"
                f"{hyperlift.ENV_KEY}={KEY}\n{hyperlift.ENV_SECRET}={SECRET}\n"
            )
        requests = []

        def fake_urlopen(req, timeout=None):
            requests.append(req)
            return FakeResponse(APP_PAYLOAD)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = hyperlift.main(["get", APP])
        self.assertEqual(code, 0)
        self.assertTrue(requests[0].full_url.startswith(hyperlift.DEFAULT_BASE_URL))
        self.assertNotIn("evil.example", requests[0].full_url)


class ReadOperationTests(unittest.TestCase):
    def test_list_url_and_pagination_params(self):
        payload = {"items": [APP_PAYLOAD], "total": 1}
        code, out, _, requests = run_main(["list", "--take", "5", "--skip", "10"], [payload])
        self.assertEqual(code, 0)
        url = requests[0].full_url
        self.assertIn("/hyperlift/applications?", url)
        self.assertIn("take=5", url)
        self.assertIn("skip=10", url)
        self.assertEqual(json.loads(out)["total"], 1)

    def test_list_all_pages(self):
        first = {"items": [dict(APP_PAYLOAD, id=f"app-{i}") for i in range(100)], "total": 150}
        second = {"items": [dict(APP_PAYLOAD, id=f"app-{100 + i}") for i in range(50)], "total": 150}
        code, out, _, requests = run_main(["list", "--all"], [first, second])
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertIn("skip=100", requests[1].full_url)
        self.assertEqual(len(json.loads(out)["items"]), 150)

    def test_osc_and_control_chars_stripped_from_logs(self):
        nasty = "\x1b]0;evil title\x07before\x1b]8;;https://evil.example\x1b\\link\x07\x08\x00after"
        page = {"items": [{"message": nasty}], "finished": True}
        code, out, _, _ = run_main(["logs", APP, "-o", "text"], [page])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x00", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_ansi_stripped_from_log_output(self):
        colored = "\x1b[36mINFO\x1b[0m[0001] Retrieving image manifest"
        page = {"items": [{"message": colored, "timestamp": "t1"}], "finished": True}
        # text mode
        code, out, _, _ = run_main(["logs", APP, "-o", "text"], [page])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertIn("INFO[0001] Retrieving image manifest", out)
        # json mode
        code, out, _, _ = run_main(["logs", APP], [page])
        self.assertEqual(code, 0)
        self.assertNotIn("\\u001b", out)
        self.assertIn("INFO[0001]", out)

    def test_logs_cursor_forwarded(self):
        page = {"items": [{"message": "hello", "timestamp": "t1"}], "cursor": "c2", "finished": False}
        code, _, _, requests = run_main(["logs", APP, "--cursor", "c1", "--take", "50"], [page])
        self.assertEqual(code, 0)
        url = requests[0].full_url
        self.assertIn(f"/hyperlift/applications/{APP}/logs?", url)
        self.assertIn("cursor=c1", url)
        self.assertIn("take=50", url)

    def test_build_logs_follow_stops_on_finished(self):
        pages = [
            {"items": [{"message": "step 1"}], "cursor": "c1", "finished": False},
            {"items": [{"message": "step 2"}], "cursor": "c2", "finished": True},
        ]
        code, out, _, requests = run_main(
            ["build-logs", APP, "--follow", "--interval", "0", "--timeout", "30"], pages
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertIn("cursor=c1", requests[1].full_url)  # newest cursor passed back: no duplicates
        self.assertIn("step 1", out)
        self.assertIn("step 2", out)

    def test_metrics_query_construction(self):
        payload = {"metrics": [{"name": "memoryUsageBytes", "unit": "bytes", "samples": []}]}
        code, _, _, requests = run_main(
            [
                "metrics", APP,
                "--start", "2026-01-01T00:00:00Z",
                "--end", "2026-01-01T01:00:00Z",
                "--interval", "1m",
                "--metrics", "memoryUsageBytes,cpuUsagePercentage",
            ],
            [payload],
        )
        self.assertEqual(code, 0)
        url = requests[0].full_url
        self.assertIn("startDate=2026-01-01T00%3A00%3A00.000Z", url)
        self.assertIn("endDate=2026-01-01T01%3A00%3A00.000Z", url)
        self.assertIn("interval=1m", url)
        self.assertIn("metrics=memoryUsageBytes%2CcpuUsagePercentage", url)

    def test_metrics_rejects_too_many_intervals(self):
        code, _, err, requests = run_main(
            ["metrics", APP, "--last", "1d", "--interval", "10s"], []
        )
        self.assertEqual(code, hyperlift.EXIT_USAGE)
        self.assertEqual(len(requests), 0)
        self.assertIn("1500", err)

    def test_metrics_rejects_unknown_metric(self):
        code, _, err, _ = run_main(["metrics", APP, "--metrics", "bogusMetric"], [])
        self.assertEqual(code, hyperlift.EXIT_USAGE)
        self.assertIn("bogusMetric", err)


class ErrorHandlingTests(unittest.TestCase):
    def test_rate_limit_429(self):
        error = http_error(
            429,
            {"detail": "rate limit exceeded"},
            {"Retry-After": "42", "spaceship-error-code": "business.rateLimit"},
        )
        code, _, err, _ = run_main(["get", APP], [error])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("429", err)
        self.assertIn("42", err)

    def test_permission_403_names_scope(self):
        code, _, err, _ = run_main(["restart", APP, "--yes"], [http_error(403)])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("hyperlift:execute", err)

    def test_env_403_names_manage_scope(self):
        code, _, err, _ = run_main(["env-get", APP], [http_error(403)])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("hyperlift:manage", err)

    def test_server_error_500(self):
        code, _, err, _ = run_main(["get", APP], [http_error(500, {"detail": "boom"})])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("500", err)
        self.assertIn("boom", err)

    def test_not_found_404(self):
        code, _, err, _ = run_main(["get", APP], [http_error(404)])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("404", err)

    def test_network_error(self):
        code, _, err, _ = run_main(["get", APP], [urllib.error.URLError("dns failure")])
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertIn("network error", err)

    def test_secret_redacted_from_errors(self):
        code, _, err, _ = run_main(
            ["get", APP], [http_error(400, {"detail": f"echoing {SECRET} back"})]
        )
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertNotIn(SECRET, err)
        self.assertIn("***", err)


class ConfirmationTests(unittest.TestCase):
    def test_mutations_require_yes(self):
        for argv in (
            ["build", APP],
            ["restart", APP],
            ["start", APP],
            ["stop", APP],
            ["scale", APP, "--scale", "1"],
        ):
            code, _, err, requests = run_main(argv, [])
            self.assertEqual(code, hyperlift.EXIT_CONFIRM, argv)
            self.assertEqual(len(requests), 0, argv)  # no API call without --yes
            self.assertIn("--yes", err)

    def test_build_posts_with_yes(self):
        code, _, _, requests = run_main(["build", APP, "--yes"], [{"id": APP}])
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertTrue(requests[0].full_url.endswith(f"/hyperlift/applications/{APP}/build"))

    def test_stop_sends_scale_zero(self):
        code, _, _, requests = run_main(["stop", APP, "--yes"], [{"id": APP}])
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].get_method(), "PUT")
        self.assertEqual(json.loads(requests[0].data), {"scale": 0})

    def test_scale_rejects_out_of_range(self):
        code, _, _, requests = run_main(["scale", APP, "--scale", "2"], [])
        self.assertEqual(code, hyperlift.EXIT_USAGE)
        self.assertEqual(len(requests), 0)


class EnvironmentMergeTests(unittest.TestCase):
    def test_merge_preserves_unspecified(self):
        merged, plan = hyperlift.merge_environment(
            {"A": "1", "B": "2"}, sets={"C": "3", "B": "9"}, removes=["A"]
        )
        self.assertEqual(merged, {"B": "9", "C": "3"})
        self.assertEqual(plan["added"], ["C"])
        self.assertEqual(plan["updated"], ["B"])
        self.assertEqual(plan["removed"], ["A"])
        self.assertEqual(plan["kept"], [])

    def test_merge_normalizes_names(self):
        merged, plan = hyperlift.merge_environment({"DB_USER": "old"}, {"db-user": "new"}, [])
        self.assertEqual(merged, {"DB_USER": "new"})
        self.assertEqual(plan["updated"], ["DB_USER"])

    def test_remove_normalizes_names(self):
        merged, plan = hyperlift.merge_environment({"DB_USER": "x", "KEEP": "y"}, {}, ["db user"])
        self.assertEqual(merged, {"KEEP": "y"})
        self.assertEqual(plan["removed"], ["DB_USER"])
        self.assertEqual(plan["kept"], ["KEEP"])

    def test_validation_rejects_too_many_vars(self):
        items = {f"VAR_{i}": "x" for i in range(21)}
        with self.assertRaises(ValueError):
            hyperlift.validate_environment(items)

    def test_validation_rejects_leading_digit(self):
        with self.assertRaises(ValueError):
            hyperlift.validate_env_name("1BAD")

    def test_validation_rejects_long_value(self):
        with self.assertRaises(ValueError) as ctx:
            hyperlift.validate_environment({"NAME": "x" * 16385})
        self.assertNotIn("x" * 100, str(ctx.exception))  # value not echoed in error

    def test_empty_value_allowed(self):
        merged, _ = hyperlift.merge_environment({}, {"APPLICATION_PORT": ""}, [])
        hyperlift.validate_environment(merged)
        self.assertEqual(merged, {"APPLICATION_PORT": ""})


class EnvironmentCommandTests(unittest.TestCase):
    def test_env_set_dry_run_shows_plan_without_calling_put(self):
        current = {"id": APP, "items": {"KEEP_ME": "kept-value"}}
        code, out, err, requests = run_main(["env-set", APP, "--set", "NEW=secret-val"], [current])
        self.assertEqual(code, hyperlift.EXIT_CONFIRM)
        self.assertEqual(len(requests), 1)  # only the GET, no PUT
        self.assertIn("added (1): NEW", err)
        self.assertIn("kept (1): KEEP_ME", err)

    def test_env_set_merges_and_replaces_full_set(self):
        current = {"id": APP, "items": {"KEEP_ME": "kept-value", "DROP_ME": "bye"}}
        code, _, _, requests = run_main(
            ["env-set", APP, "--set", "NEW=v1", "--remove", "DROP_ME", "--yes"],
            [current, {"id": APP}],
        )
        self.assertEqual(code, 0)
        put = requests[1]
        self.assertEqual(put.get_method(), "PUT")
        body = json.loads(put.data)
        self.assertEqual(body, {"items": {"KEEP_ME": "kept-value", "NEW": "v1"}})

    def test_env_set_never_prints_values(self):
        secret_value = "s3cr3t-value-xyz"
        current = {"id": APP, "items": {"EXISTING": "existing-secret"}}
        code, out, err, _ = run_main(
            ["env-set", APP, "--set", f"TOKEN={secret_value}", "--yes"],
            [current, {"id": APP}],
        )
        self.assertEqual(code, 0)
        combined = out + err
        self.assertNotIn(secret_value, combined)
        self.assertNotIn("existing-secret", combined)
        self.assertIn("TOKEN", combined)

    def test_env_get_masks_values_by_default(self):
        current = {"id": APP, "items": {"TOKEN": "super-secret"}}
        code, out, err, _ = run_main(["env-get", APP], [current])
        self.assertEqual(code, 0)
        self.assertNotIn("super-secret", out + err)
        self.assertIn("TOKEN", out)

    def test_env_get_show_values(self):
        current = {"id": APP, "items": {"TOKEN": "super-secret"}}
        code, out, _, _ = run_main(["env-get", APP, "--show-values"], [current])
        self.assertEqual(code, 0)
        self.assertIn("super-secret", out)

    def test_env_values_not_leaked_via_api_error(self):
        # A failing update whose API error echoes a submitted value back must
        # not print that value.
        leaked = "super-secret-token-abc"
        current = {"id": APP, "items": {"EXISTING": "existing-secret"}}
        error = http_error(422, {"detail": f"value {leaked} is not allowed"})
        code, out, err, _ = run_main(
            ["env-set", APP, "--set", f"TOKEN={leaked}", "--yes"], [current, error]
        )
        self.assertEqual(code, hyperlift.EXIT_ERROR)
        self.assertNotIn(leaked, out + err)
        self.assertNotIn("existing-secret", out + err)

    def test_env_set_requires_changes(self):
        code, _, err, requests = run_main(["env-set", APP], [])
        self.assertEqual(code, hyperlift.EXIT_USAGE)
        self.assertEqual(len(requests), 0)
        self.assertIn("--set", err)


if __name__ == "__main__":
    unittest.main()
