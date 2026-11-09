#!/usr/bin/env python3
"""A fixed-generation M2M probe. No admin API or Kubernetes access."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import signal
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

OAUTH_ERRORS = frozenset({"invalid_client", "unauthorized_client", "invalid_request", "invalid_grant", "invalid_scope", "unsupported_grant_type", "server_error", "temporarily_unavailable", "access_denied"})


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_label(value: str) -> str:
    return "".join(c for c in value[:128] if c.isalnum() or c in "-._")


@dataclasses.dataclass(frozen=True)
class Config:
    token_url: str
    client_id: str
    client_secret: str = dataclasses.field(repr=False)
    generation: str = "v0001"
    pod: str = "unknown"
    role: str = "main"
    ca_file: str | None = None
    interval: float = 2.0
    timeout: float = 5.0
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Config":
        # Captured once before the process begins. No credential reload/fallback.
        url = os.environ["TOKEN_URL"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("TOKEN_URL must be an HTTPS URL without user information or fragment")
        secret = os.environ["CLIENT_SECRET"]
        if not secret:
            raise ValueError("CLIENT_SECRET is required")
        interval, timeout = float(os.getenv("PROBE_INTERVAL_SECONDS", "2")), float(os.getenv("REQUEST_TIMEOUT_SECONDS", "5"))
        if interval < 0.1 or not 0 < timeout <= 30:
            raise ValueError("invalid probe interval or request timeout")
        role = os.getenv("PROBE_ROLE", "main")
        if role not in {"main", "old"}:
            raise ValueError("PROBE_ROLE must be main or old")
        return cls(url, os.getenv("CLIENT_ID", "catalogo-api"), secret,
                   safe_label(os.environ["GENERATION"]), safe_label(os.getenv("POD_NAME", socket.gethostname())),
                   role, os.getenv("TLS_CA_FILE") or None, interval, timeout, int(os.getenv("PORT", "8080")))


@dataclasses.dataclass(frozen=True)
class Result:
    http_status: int | None
    outcome: str
    oauth_error: str | None
    latency_ms: float

    @property
    def success(self) -> bool:
        return self.http_status == 200 and self.outcome == "success"


def oauth_error(body: bytes) -> str:
    try:
        value = json.loads(body).get("error")
        return value if isinstance(value, str) and value in OAUTH_ERRORS else "unrecognized_oauth_error"
    except (ValueError, AttributeError, TypeError):
        return "non_oauth_response"


def request_token(config: Config, opener: Callable | None = None) -> Result:
    """Always sends a new client_credentials request; never returns an access token."""
    started = time.monotonic()
    status, outcome, error = None, "network_error", None
    try:
        context = ssl.create_default_context(cafile=config.ca_file)
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": config.client_id,
                                      "client_secret": config.client_secret}).encode()
        request = urllib.request.Request(config.token_url, body, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
        # Reject redirects: never forward credentials to a different endpoint.
        if opener is None:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            transport = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
            open_request = transport.open
        else:
            open_request = opener
        with open_request(request, timeout=config.timeout) as response:
            status = response.status
            payload = json.loads(response.read(65536))
            # Check the successful response shape, then discard all sensitive data.
            if status == 200 and isinstance(payload, dict) and isinstance(payload.get("access_token"), str) and payload["access_token"]:
                outcome = "success"
            else:
                outcome = "invalid_token_response"
            del payload
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = oauth_error(exc.read(65536))
        exc.close()
        outcome = "oauth_error" if error in OAUTH_ERRORS else "http_error"
    except ssl.SSLError:
        outcome = "tls_error"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            outcome = "tls_error"
        elif isinstance(exc.reason, (TimeoutError, socket.timeout)):
            outcome = "timeout"
        else:
            outcome = "network_error"
    except (TimeoutError, socket.timeout):
        outcome = "timeout"
    except (ValueError, TypeError, AttributeError):
        outcome = "invalid_token_response"
    except OSError:
        outcome = "network_error"
    return Result(status, outcome, error, round((time.monotonic() - started) * 1000, 3))


class Probe:
    def __init__(self, config: Config, sender: Callable = request_token, logger: Callable | None = None):
        self.config, self.sender = config, sender
        self.logger = logger or (lambda event: print(json.dumps(event, separators=(",", ":")), flush=True))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.started = utc()
        self.heartbeat = time.monotonic()
        self.attempts = self.successes = self.failures = 0
        self.initial_authentication = False
        self.last_success_at = None
        self.last: dict = {}
        self.outcomes: dict[str, int] = {}

    def once(self) -> Result:
        result = self.sender(self.config)
        event = {"timestamp_utc": utc(), "event": "token_attempt", "pod": self.config.pod,
                 "generation": self.config.generation, "role": self.config.role,
                 "http_status": result.http_status, "outcome": result.outcome,
                 "oauth_error": result.oauth_error, "latency_ms": result.latency_ms}
        with self.lock:
            self.heartbeat = time.monotonic()
            self.attempts += 1
            self.successes += int(result.success)
            self.failures += int(not result.success)
            self.initial_authentication |= result.success
            if result.success:
                self.last_success_at = time.time()
            self.outcomes[result.outcome] = self.outcomes.get(result.outcome, 0) + 1
            self.last = event
        self.logger(event)
        return result

    def snapshot(self) -> dict:
        with self.lock:
            return {"started_utc": self.started, "pod": self.config.pod, "generation": self.config.generation,
                    "role": self.config.role, "attempts": self.attempts, "successes": self.successes,
                    "failures": self.failures, "initial_authentication": self.initial_authentication,
                    "latest_authentication_ok": self.last.get("outcome") == "success", "last": dict(self.last),
                    "last_http": self.last.get("http_status"), "last_success_at": self.last_success_at,
                    "last_result": self.last.get("outcome"),
                    "shutdown": self.stop.is_set()}

    def live(self) -> bool:
        return not self.stop.is_set() and time.monotonic() - self.heartbeat < self.config.timeout + self.config.interval + 10

    def ready(self) -> bool:
        # Initial success gates rollout. Later auth errors remain observable without restarting the process.
        return self.initial_authentication and not self.stop.is_set()

    def metrics(self) -> str:
        snap = self.snapshot()
        labels = f'pod="{self.config.pod}",generation="{self.config.generation}",role="{self.config.role}"'
        lines = ["# TYPE m2m_token_attempts_total counter", f'm2m_token_attempts_total{{{labels}}} {snap["attempts"]}',
                 "# TYPE m2m_token_successes_total counter", f'm2m_token_successes_total{{{labels}}} {snap["successes"]}',
                 "# TYPE m2m_token_failures_total counter", f'm2m_token_failures_total{{{labels}}} {snap["failures"]}',
                 "# TYPE m2m_latest_authentication_ok gauge", f'm2m_latest_authentication_ok{{{labels}}} {int(snap["latest_authentication_ok"])}']
        return "\n".join(lines) + "\n"

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            self.once()
            self.stop.wait(max(0, self.config.interval - (time.monotonic() - started)))
        self.logger({"timestamp_utc": utc(), "event": "shutdown", "pod": self.config.pod,
                     "generation": self.config.generation, "role": self.config.role, "reason": "normal"})


DASHBOARD = b'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>M2M probe local</title><style>body{font:16px system-ui;margin:3rem;max-width:64rem;background:#101820;color:#e6edf3}pre{padding:1rem;background:#1c2934;white-space:pre-wrap}p{color:#b9c6d0}</style><h1>Autentica\xc3\xa7\xc3\xb5es M2M</h1><p>Este painel acompanha somente o pod selecionado pelo port-forward. A sonda antiga tem indicadores separados. Cada tentativa solicita um novo token.</p><pre id="state">Carregando...</pre><script>async function update(){try{let r=await fetch('/status');document.getElementById('state').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.getElementById('state').textContent='Conex\xc3\xa3o indispon\xc3\xadvel'}}update();setInterval(update,2000)</script></html>'''


def handler_for(probe: Probe):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                status, body, kind = (200 if probe.live() else 503), b"process health\n", "text/plain"
            elif self.path == "/readyz":
                status, body, kind = (200 if probe.ready() else 503), b"initial authentication\n", "text/plain"
            elif self.path == "/status":
                status, body, kind = 200, json.dumps(probe.snapshot()).encode(), "application/json"
            elif self.path == "/metrics":
                status, body, kind = 200, probe.metrics().encode(), "text/plain; version=0.0.4"
            elif self.path == "/":
                status, body, kind = 200, DASHBOARD, "text/html; charset=utf-8"
            else:
                status, body, kind = 404, b"not found\n", "text/plain"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass
    return Handler


def main() -> int:
    try:
        config = Config.from_env()
        # Fail before serving if a configured CA cannot be read/parsed.
        ssl.create_default_context(cafile=config.ca_file)
    except (ValueError, KeyError, OSError, ssl.SSLError):
        print(json.dumps({"timestamp_utc": utc(), "event": "configuration_error"}), flush=True)
        return 2
    probe = Probe(config)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), handler_for(probe))
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: probe.stop.set())
    signal.signal(signal.SIGINT, lambda *_: probe.stop.set())
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        probe.run()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
