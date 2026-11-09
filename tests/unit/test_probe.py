import secrets
TEST_VALUE = secrets.token_urlsafe(18)
TEST_FIRST = secrets.token_urlsafe(18)
TEST_SECOND = secrets.token_urlsafe(18)
"""Independent checks for probe credentials, request freshness, health and output hygiene."""
import importlib.util
import io
import json
from pathlib import Path
import ssl
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

PATH = Path(__file__).resolve().parents[2] / "apps/m2m-probe/probe.py"
spec = importlib.util.spec_from_file_location("m2m_probe", PATH)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class Response(io.BytesIO):
    status = 200


class ProbeTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(token_url="https://rhbk.test/realms/lab/protocol/openid-connect/token", client_id="catalogo-api", client_secret=TEST_VALUE, generation="v0001", pod="pod-1")
        values.update(changes)
        return probe.Config(**values)

    def test_each_attempt_sends_a_fresh_client_credentials_request(self):
        sent = []
        def transport(request, timeout):
            sent.append(request)
            return Response(json.dumps({"access_token": "unit-test-token"}).encode())
        cfg = self.config()
        self.assertTrue(probe.request_token(cfg, transport).success)
        self.assertTrue(probe.request_token(cfg, transport).success)
        self.assertIsNot(sent[0], sent[1])
        for request in sent:
            form = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(form, {"grant_type": ["client_credentials"], "client_id": [cfg.client_id], "client_secret": [cfg.client_secret]})
            self.assertNotIn("Authorization", request.headers)

    def test_secret_is_read_once_and_hidden_from_repr(self):
        env = {"TOKEN_URL": "https://rhbk.test/token", "CLIENT_SECRET": TEST_FIRST, "GENERATION": "v0001"}
        with patch.dict("os.environ", env, clear=True):
            cfg = probe.Config.from_env()
            with patch.dict("os.environ", {"CLIENT_SECRET": TEST_SECOND, "GENERATION": "v0002"}):
                self.assertEqual(cfg.client_secret, TEST_FIRST)
                self.assertEqual(cfg.generation, "v0001")
        self.assertNotIn(cfg.client_secret, repr(cfg))

    def test_plaintext_token_url_refused(self):
        with patch.dict("os.environ", {"TOKEN_URL": "http://rhbk.test/token", "CLIENT_SECRET": TEST_VALUE, "GENERATION": "v0001"}, clear=True):
            with self.assertRaises(ValueError):
                probe.Config.from_env()

    def test_untrusted_error_message_cannot_leak_values(self):
        def transport(request, timeout):
            body = json.dumps({"error": TEST_VALUE, "error_description": "unit-test-token"}).encode()
            raise urllib.error.HTTPError(request.full_url, 401, "do not log unit-test-value", {}, io.BytesIO(body))
        result = probe.request_token(self.config(), transport)
        self.assertEqual(result.http_status, 401)
        self.assertEqual(result.oauth_error, "unrecognized_oauth_error")
        self.assertNotIn("unit-test", repr(result))

    def test_oauth_rejection_is_distinct_from_tls_network_and_timeout(self):
        cases = [(ssl.SSLError("unit-test-sensitive-detail"), "tls_error"),
                 (urllib.error.URLError(ssl.SSLError("bad chain")), "tls_error"),
                 (urllib.error.URLError(OSError("network detail")), "network_error"),
                 (TimeoutError(), "timeout")]
        for exception, expected in cases:
            with self.subTest(expected=expected):
                def fail(*args, **kwargs):
                    raise exception
                result = probe.request_token(self.config(), fail)
                self.assertEqual(result.outcome, expected)
                self.assertIsNone(result.oauth_error)
        def denied(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"invalid_client"}'))
        self.assertEqual(probe.request_token(self.config(), denied).oauth_error, "invalid_client")

    def test_readiness_requires_initial_success_and_auth_failure_does_not_kill_liveness(self):
        results = iter([probe.Result(401,"oauth_error","invalid_client",1), probe.Result(200,"success",None,1), probe.Result(401,"oauth_error","invalid_client",1)])
        p = probe.Probe(self.config(), lambda _: next(results), lambda _: None)
        self.assertFalse(p.ready())
        p.once()
        self.assertFalse(p.ready())
        p.once()
        self.assertTrue(p.ready())
        first_success = p.snapshot()["last_success_at"]
        p.once()
        self.assertTrue(p.ready())
        self.assertTrue(p.live())
        self.assertFalse(p.snapshot()["latest_authentication_ok"])
        self.assertEqual(p.snapshot()["last_success_at"], first_success)
        self.assertEqual(p.snapshot()["last_http"], 401)

    def test_output_and_metrics_never_contain_credential_or_token(self):
        events = []
        p = probe.Probe(self.config(role="old"), lambda _: probe.Result(200,"success",None,1), events.append)
        p.once()
        output = json.dumps(events) + json.dumps(p.snapshot()) + p.metrics()
        self.assertNotIn(p.config.client_secret, output)
        self.assertNotIn("access_token", output)
        self.assertIn('role="old"', p.metrics())
        self.assertEqual(p.snapshot()["successes"], 1)

    def test_http_health_and_snapshot_are_sanitized(self):
        p = probe.Probe(self.config(), lambda _: probe.Result(200,"success",None,1), lambda _: None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), probe.handler_for(p))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(url + "/readyz")
            self.assertEqual(failure.exception.code, 503)
            failure.exception.close()
            p.once()
            with urllib.request.urlopen(url + "/status") as response:
                status = json.load(response)
                self.assertEqual(status["generation"], "v0001")
                self.assertEqual(status["last_http"], 200)
                self.assertGreater(status["last_success_at"], 0)
                self.assertNotIn(TEST_VALUE, json.dumps(status))
            with urllib.request.urlopen(url + "/readyz") as response:
                self.assertEqual(response.status, 200)
            with urllib.request.urlopen(url + "/healthz") as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_normal_shutdown_has_no_authentication_failure(self):
        events = []
        p = probe.Probe(self.config(), logger=events.append)
        p.stop.set()
        p.run()
        self.assertEqual(events[0]["event"], "shutdown")
        self.assertEqual(events[0]["reason"], "normal")
        self.assertEqual(p.failures, 0)
        self.assertFalse(p.live())
        self.assertFalse(p.ready())


if __name__ == "__main__":
    unittest.main()
