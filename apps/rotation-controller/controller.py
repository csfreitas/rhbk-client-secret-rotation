#!/usr/bin/env python3
"""External, crash-aware RHBK client-secret rotation. Python standard library only.

Never retry a secret-generation POST. Credentials exist only in memory and in
immutable Kubernetes Secrets. State and stdout contain allowlisted metadata.
"""
import argparse
import base64
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LABELS = {"app.kubernetes.io/part-of": "rhbk-secret-rotation-lab", "lab.rhbk/owner": "client-secret-rotation"}
META_KEYS = ("client.secret.creation.time", "client.secret.expiration.time", "client.secret.rotated.creation.time", "client.secret.rotated.expiration.time")
EXPIRY_REJECTION_ERRORS = {"invalid_client", "unauthorized_client"}
OAUTH_ERRORS = {"invalid_client", "unauthorized_client", "invalid_grant", "invalid_request", "invalid_scope", "temporarily_unavailable", "server_error", "access_denied"}
HTTP_TIMEOUT_SECONDS = 10
TERMINAL = {"IDLE", "COMPLETE"}


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_micro(epoch=None):
    """Kubernetes Lease MicroTime requires exactly six fractional digits."""
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("invalid epoch timestamp")
    return number / 1000 if number > 10**11 else number


def event(name, **fields):
    print(json.dumps({"timestamp": utc(), "event": name, **fields}, sort_keys=True), flush=True)


class SafeError(Exception):
    """The message must be a fixed, nonsensitive diagnostic, never a response."""


class APIError(SafeError):
    def __init__(self, status=0, category="api_error", oauth=None):
        self.status, self.category, self.oauth = status, category, oauth
        super().__init__(f"{category}: HTTP {status}" + (f" ({oauth})" if oauth else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise APIError(0, "redirect_refused")


class HTTP:
    def __init__(self, base, ca=None, bearer=None):
        if not base.startswith("https://"):
            raise SafeError("HTTPS is required")
        self.base, self.bearer = base.rstrip("/"), bearer
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)), NoRedirect())

    def request(self, method, path, body=None, form=False, bearer=None):
        headers = {"Accept": "application/json"}
        token = bearer if bearer is not None else (self.bearer() if callable(self.bearer) else self.bearer)
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        start = time.time()
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
                payload = json.loads(raw) if raw else {}
                return response.status, payload, {"started": start, "finished": time.time(), "date": response.headers.get("Date")}
        except urllib.error.HTTPError as exc:
            oauth = None
            try:
                candidate = json.loads(exc.read(16384)).get("error")
                oauth = candidate if candidate in OAUTH_ERRORS else None
            except (ValueError, TypeError, AttributeError):
                pass
            raise APIError(exc.code, "http_error", oauth) from None
        except urllib.error.URLError as exc:
            kind = "tls_error" if isinstance(exc.reason, ssl.SSLError) else "network_error"
            raise APIError(0, kind) from None
        except (TimeoutError, socket.timeout):
            raise APIError(0, "timeout") from None
        except (ValueError, UnicodeError):
            raise APIError(0, "invalid_json") from None


class Keycloak:
    def __init__(self):
        self.realm = os.getenv("REALM", "lab-secret-rotation")
        self.admin_client = os.getenv("ADMIN_CLIENT_ID", "rotation-admin")
        self.secret_file = Path(os.environ["ADMIN_CLIENT_SECRET_FILE"])
        self.http = HTTP(os.environ["RHBK_URL"], os.getenv("RHBK_CA_FILE"))
        self.token, self.token_until = None, 0
        self.root = "/admin/realms/" + urllib.parse.quote(self.realm, safe="")
        self.token_path = "/realms/" + urllib.parse.quote(self.realm, safe="") + "/protocol/openid-connect/token"

    def admin_token(self):
        if time.time() >= self.token_until:
            _, result, _ = self.http.request("POST", self.token_path, {"grant_type": "client_credentials", "client_id": self.admin_client, "client_secret": self.secret_file.read_text().strip()}, form=True)
            self.token, self.token_until = result["access_token"], time.time() + max(1, int(result.get("expires_in", 30)) - 10)
        return self.token

    def admin(self, method, path, body=None):
        return self.http.request(method, self.root + path, body, bearer=self.admin_token())

    def resolve(self, client_id):
        _, clients, _ = self.admin("GET", "/clients?clientId=" + urllib.parse.quote(client_id, safe="") + "&exact=true")
        matches = [c for c in clients if c.get("clientId") == client_id]
        if len(matches) != 1:
            raise SafeError("target client is missing or ambiguous")
        return matches[0]["id"]

    def metadata(self, client_uuid):
        _, client, _ = self.admin("GET", "/clients/" + client_uuid)
        attrs = client.get("attributes", {})
        result = {}
        for key in META_KEYS:
            if key in attrs:
                try:
                    result[key] = timestamp(attrs[key])
                except (ValueError, TypeError):
                    raise SafeError("invalid rotation timestamp") from None
        return result

    def secret(self, client_uuid, rotated=False):
        try:
            _, result, _ = self.admin("GET", "/clients/" + client_uuid + "/client-secret" + ("/rotated" if rotated else ""))
            return result.get("value")
        except APIError as exc:
            if rotated and exc.status == 404:
                return None
            raise

    def rotate(self, client_uuid):
        status, result, interval = self.admin("POST", "/clients/" + client_uuid + "/client-secret")
        interval["http"] = status
        value = result.get("value")
        if not value:
            raise SafeError("rotation response has no credential; reconciliation required")
        return value, interval

    def invalidate_rotated(self, client_uuid):
        try:
            self.admin("DELETE", "/clients/" + client_uuid + "/client-secret/rotated")
        except APIError as exc:
            if exc.status != 404:
                raise

    def authenticate(self, client_id, secret):
        started = time.monotonic()
        try:
            status, response, _ = self.http.request("POST", self.token_path, {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}, form=True)
            ok = status == 200 and bool(response.get("access_token"))
            result = {"http": status, "ok": ok, "error": None if ok else "token_missing"}
        except APIError as exc:
            result = {"http": exc.status, "ok": False, "error": exc.oauth or exc.category}
        return {**result, "latency_ms": round((time.monotonic() - started) * 1000, 2), "timestamp": utc()}


class Kube:
    def __init__(self):
        self.namespace = os.getenv("NAMESPACE", "rhbk-rotation-lab")
        if self.namespace != "rhbk-rotation-lab":
            raise SafeError("controller is restricted to rhbk-rotation-lab")
        sa = Path(os.getenv("SERVICE_ACCOUNT_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"))
        self.http = HTTP("https://" + os.environ["KUBERNETES_SERVICE_HOST"] + ":" + os.getenv("KUBERNETES_SERVICE_PORT", "443"), str(sa / "ca.crt"), lambda: (sa / "token").read_text().strip())
        self.core = "/api/v1/namespaces/" + self.namespace
        self.apps = "/apis/apps/v1/namespaces/" + self.namespace
        self.coordination = "/apis/coordination.k8s.io/v1/namespaces/" + self.namespace
        self.batch = "/apis/batch/v1/namespaces/" + self.namespace

    @staticmethod
    def owned(obj):
        if any(obj.get("metadata", {}).get("labels", {}).get(k) != v for k, v in LABELS.items()):
            raise SafeError("resource ownership mismatch")

    def get(self, path, optional=False):
        try:
            return self.http.request("GET", path)[1]
        except APIError as exc:
            if optional and exc.status == 404:
                return None
            raise

    def create(self, path, obj):
        return self.http.request("POST", path, obj)[1]

    def replace(self, path, obj):
        return self.http.request("PUT", path, obj)[1]


class Lease:
    def __init__(self, kube, name, duration=30):
        self.kube, self.name, self.duration = kube, name, duration
        self.holder = os.getenv("HOSTNAME", "manual") + "-" + str(uuid.uuid4())
        self.path = kube.coordination + "/leases/" + name
        self.stop_event, self.mutex = threading.Event(), threading.Lock()
        self.lost = False
        self.last_renew_monotonic = 0
        self.thread = None

    def acquire(self):
        obj = self.kube.get(self.path, optional=True)
        now = time.time()
        if obj:
            self.kube.owned(obj)
            spec = obj.get("spec", {})
            when = spec.get("renewTime") or spec.get("acquireTime")
            until = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() + spec.get("leaseDurationSeconds", self.duration) if when else 0
            if spec.get("holderIdentity") and until + 5 > now:
                raise SafeError("another controller holds the client lease")
        else:
            obj = {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"name": self.name, "labels": LABELS}}
        obj["spec"] = {"holderIdentity": self.holder, "leaseDurationSeconds": self.duration, "acquireTime": utc_micro(), "renewTime": utc_micro()}
        if obj["metadata"].get("resourceVersion"):
            self.kube.replace(self.path, obj)
        else:
            self.kube.create(self.kube.coordination + "/leases", obj)
        self.last_renew_monotonic = time.monotonic()
        self.thread = threading.Thread(target=self._renew_loop, daemon=True)
        self.thread.start()
        return self

    def _renew_loop(self):
        while not self.stop_event.wait(self.duration / 5):
            try:
                self.renew()
            except Exception:
                self.lost = True
                return

    def renew(self):
        with self.mutex:
            obj = self.kube.get(self.path)
            if obj.get("spec", {}).get("holderIdentity") != self.holder:
                self.lost = True
                raise SafeError("client lease lost")
            obj["spec"]["renewTime"] = utc_micro()
            self.kube.replace(self.path, obj)
            self.last_renew_monotonic = time.monotonic()

    def check(self):
        if self.lost or time.monotonic() - self.last_renew_monotonic >= self.duration - 5:
            raise SafeError("lease unavailable; refusing mutation")

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)
        try:
            with self.mutex:
                obj = self.kube.get(self.path)
                if obj.get("spec", {}).get("holderIdentity") == self.holder:
                    obj["spec"]["holderIdentity"] = ""
                    self.kube.replace(self.path, obj)
        except SafeError:
            pass


class State:
    def __init__(self, kube, name):
        self.kube, self.path = kube, kube.core + "/configmaps/" + name
        self.object = kube.get(self.path)
        kube.owned(self.object)
        self.value = json.loads(self.object["data"]["state.json"])

    def save(self, **changes):
        self.value.update(changes)
        self.value["updated_at"] = time.time()
        self.object["data"]["state.json"] = json.dumps(self.value, sort_keys=True)
        self.object = self.kube.replace(self.path, self.object)


class Controller:
    def __init__(self, kube, kc, state, lease, clock=time.time, sleeper=time.sleep):
        self.kube, self.kc, self.state, self.lease = kube, kc, state, lease
        self.clock, self.sleep = clock, sleeper
        self.client = os.getenv("TARGET_CLIENT_ID", "catalogo-api")
        if self.client != "catalogo-api":
            raise SafeError("controller target must be catalogo-api; tests use separate clients")
        self.deployment = os.getenv("DEPLOYMENT_NAME", "m2m-probe")
        self.hold = int(os.getenv("HOLD_SECONDS", "45"))
        self.rollout_budget = int(os.getenv("ROLLOUT_SECONDS", "60"))
        self.margin = int(os.getenv("MARGIN_SECONDS", "15"))
        self.rotated_ttl = int(os.getenv("ROTATED_TTL", "120"))
        self.interval = int(os.getenv("ROTATE_INTERVAL", "180"))
        self.epsilon = int(os.getenv("EPSILON_SECONDS", "5"))
        self.pre_post_budget = max(15, HTTP_TIMEOUT_SECONDS + self.epsilon)
        if self.hold + self.rollout_budget + self.margin > self.rotated_ttl:
            raise SafeError("hold + rollout + margin exceeds rotated TTL")
        self.uuid = None

    def save(self, **changes):
        self.lease.check()
        self.state.save(**changes)
        if "phase" in changes:
            event("state_transition", phase=changes["phase"], generation=self.state.value.get("generation"), next_generation=self.state.value.get("next_generation"))

    def block(self, reason):
        self.save(phase="BLOCKED", suspended=True, blocked_reason=reason)
        raise SafeError(reason)

    def credential(self, name):
        if not name.startswith("catalogo-api-cred-v"):
            raise SafeError("unexpected credential object name")
        obj = self.kube.get(self.kube.core + "/secrets/" + name)
        self.kube.owned(obj)
        return base64.b64decode(obj["data"]["client-secret"]).decode()

    def deployment_object(self):
        obj = self.kube.get(self.kube.apps + "/deployments/" + self.deployment)
        self.kube.owned(obj)
        return obj

    def verify_pods(self, generation):
        dep = self.deployment_object()
        desired = dep["spec"].get("replicas", 1)
        st = dep.get("status", {})
        if st.get("observedGeneration", 0) < dep["metadata"].get("generation", 1) or any(st.get(k, 0) != desired for k in ("updatedReplicas", "availableReplicas", "replicas")):
            return False
        selector = urllib.parse.quote("app.kubernetes.io/name=m2m-probe", safe="")
        pods = self.kube.get(self.kube.core + "/pods?labelSelector=" + selector).get("items", [])
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        if len(active) != desired:
            return False
        for pod in active:
            self.kube.owned(pod)
            if pod["metadata"].get("labels", {}).get("lab.rhbk/generation") != f"v{generation:04d}":
                return False
            if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])):
                return False
            status = self.kube.get(self.kube.core + "/pods/" + pod["metadata"]["name"] + ":8080/proxy/status")
            last = status.get("last_success_at", 0)
            if isinstance(last, str):
                last = dt.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
            if status.get("generation") != f"v{generation:04d}" or not status.get("successes", 0) or self.clock() - last > 15 or status.get("last_http") != 200:
                return False
        return True

    def old_probe(self, generation):
        name = f"old-probe-v{generation:04d}"
        existing = self.kube.get(self.kube.core + "/pods/" + name, optional=True)
        if existing:
            self.kube.owned(existing)
            return
        dep = self.deployment_object()
        template = copy.deepcopy(dep["spec"]["template"])
        template["metadata"] = {"name": name, "labels": {**LABELS, "app.kubernetes.io/name": "old-probe", "lab.rhbk/generation": f"v{generation:04d}"}}
        spec = template["spec"]
        spec["restartPolicy"], spec["activeDeadlineSeconds"] = "Never", 360
        for container in spec["containers"]:
            if container["name"] == "probe":
                container.pop("readinessProbe", None)
                container["env"] = [e for e in container.get("env", []) if e["name"] != "PROBE_ROLE"] + [{"name": "PROBE_ROLE", "value": "old"}]
        self.lease.check()
        self.kube.create(self.kube.core + "/pods", {"apiVersion": "v1", "kind": "Pod", **template})

    def stage(self, value):
        s = self.state.value
        name = s["next_secret"]
        existing = self.kube.get(self.kube.core + "/secrets/" + name, optional=True)
        if existing:
            self.kube.owned(existing)
            if not existing.get("immutable") or base64.b64decode(existing["data"]["client-secret"]).decode() != value:
                self.block("staged credential conflicts with authoritative RHBK credential")
            return
        self.lease.check()
        if os.getenv("INJECT_FAILURE") == "secret-write":
            raise SafeError("injected Secret distribution failure")
        self.kube.create(self.kube.core + "/secrets", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "labels": {**LABELS, "lab.rhbk/generation": f"v{s['next_generation']:04d}"}}, "type": "Opaque", "immutable": True, "stringData": {"client-secret": value, "generation": f"v{s['next_generation']:04d}", "client-id": self.client}})

    def start_rollout(self):
        self.lease.check()
        dep = self.deployment_object()
        template = dep["spec"]["template"]
        template["metadata"].setdefault("labels", {})["lab.rhbk/generation"] = f"v{self.state.value['next_generation']:04d}"
        found = False
        seen = set()
        for container in template["spec"]["containers"]:
            if container["name"] == "probe":
                found = True
                for env in container.get("env", []):
                    if env["name"] in {"CLIENT_SECRET", "GENERATION", "CLIENT_ID"}:
                        seen.add(env["name"])
                        if "secretKeyRef" not in env.get("valueFrom", {}):
                            self.block("probe credential environment must reference versioned Secrets")
                        env["valueFrom"]["secretKeyRef"]["name"] = self.state.value["next_secret"]
        if not found or seen != {"CLIENT_SECRET", "GENERATION", "CLIENT_ID"}:
            self.block("probe container credential contract incomplete")
        if os.getenv("INJECT_FAILURE") == "rollout":
            raise SafeError("injected rollout failure")
        self.kube.replace(self.kube.apps + "/deployments/" + self.deployment, dep)

    def wait_until(self, epoch, callback=None):
        while self.clock() < epoch:
            self.lease.check()
            if callback:
                callback()
            self.sleep(min(2, max(0, epoch - self.clock())))

    def auth(self, credential, generation, role):
        result = self.kc.authenticate(self.client, credential)
        event("authentication", generation=f"v{generation:04d}", role=role, **result)
        return result

    def reconcile_rotation(self):
        s = self.state.value
        old = self.credential(s["old_secret"])
        current, rotated = self.kc.secret(self.uuid), self.kc.secret(self.uuid, rotated=True)
        if not current or current == old:
            self.block("ambiguous POST outcome; no automatic retry is permitted")
        if rotated != old:
            self.block("RHBK lineage changed; previous secret does not match rotated credential")
        if not self.auth(current, s["next_generation"], "reconcile")["ok"]:
            self.block("authoritative current credential does not authenticate")
        metadata = self.kc.metadata(self.uuid)
        if not all(metadata.get(k, 0) > 0 for k in (META_KEYS[0], META_KEYS[1], META_KEYS[3])):
            self.block("rotation expiration metadata unavailable")
        self.stage(current)
        observed = self.clock()
        if not s["post_started_at"] - self.epsilon <= metadata[META_KEYS[0]] <= observed + self.epsilon:
            self.block("principal creation timestamp outside recorded POST interval")
        # A lost response has no measurable finish time. Server creation metadata,
        # plus clock tolerance, provides a conservative hold anchor without adding
        # a complete new hold window on every reconciliation.
        anchor = s.get("post_finished_at")
        origin = "response_finished"
        if anchor is None:
            anchor = metadata[META_KEYS[0]] + self.epsilon
            origin = "principal_creation_plus_epsilon"
        self.save(phase="SECRET_STAGED", post_finished_at=s.get("post_finished_at"), post_observed_at=observed,
                  hold_until=anchor + self.hold, hold_origin=origin, post_metadata=metadata)

    def retire_expired_previous(self, old, new):
        """Retire only the known previous credential after S2 is durable.

        DELETE is idempotent; POST is never retried here. A lost DELETE response
        is reconciled against principal/rotated readback before any repetition.
        """
        s = self.state.value
        if self.kc.secret(self.uuid) != new:
            self.block("principal changed before expired-recovery retirement")
        if not self.auth(new, s["next_generation"], "before_invalidation")["ok"]:
            self.block("staged generation does not authenticate before expired-recovery invalidation")
        rotated = self.kc.secret(self.uuid, rotated=True)
        if rotated not in {old, None}:
            self.block("rotated lineage changed before expired-recovery retirement")
        if rotated == old:
            self.save(phase="RETIRING_OLD", old_retirement_intent_at=s.get("old_retirement_intent_at") or self.clock())
            self.lease.check()
            self.kc.invalidate_rotated(self.uuid)
            if os.getenv("INJECT_FAILURE") == "after-retire-delete":
                raise SafeError("injected crash after rotated DELETE; reconcile retirement without POST")
        if self.kc.secret(self.uuid, rotated=True) is not None:
            raise SafeError("rotated credential still present after retirement; reconcile existing cycle")
        rejection = self.auth(old, s["old_generation"], "old_after_invalidation")
        if rejection["ok"] or rejection["http"] not in {400, 401} or rejection["error"] not in EXPIRY_REJECTION_ERRORS:
            raise SafeError("old invalidation not proven by a fresh authentication rejection")
        if not self.auth(new, s["next_generation"], "main_after_invalidation")["ok"]:
            self.block("new generation failed after old invalidation")
        self.save(phase="ROLLING_OUT", rollout_started_at=self.clock(), old_retired_early=True,
                  old_retirement_verified_at=self.clock(), old_probe_result=rejection,
                  hold_skipped_reason="previous_generation_was_already_expired")

    def recover_expired(self):
        """Explicit operator recovery of a terminal, already unavailable generation.

        This is deliberately separate from normal evaluate/rotate-once. The
        external wrapper first excludes active Jobs; the same renewable Lease
        serializes this operation with scheduled/manual controller executions.
        """
        s = self.state.value
        if s["phase"] not in TERMINAL:
            raise SafeError("recovery requires terminal state; reconcile the pending cycle instead")
        cron = self.kube.get(self.kube.batch + "/cronjobs/rotation-controller")
        self.kube.owned(cron)
        if not cron["spec"].get("suspend") or not s.get("suspended"):
            raise SafeError("suspend the CronJob and persisted automation state before expired recovery")
        dep = self.deployment_object()
        template = dep["spec"]["template"]
        if template.get("metadata", {}).get("labels", {}).get("lab.rhbk/generation") != f"v{s['generation']:04d}":
            raise SafeError("deployment generation differs from terminal state; inspect before recovery")
        probe = [item for item in template["spec"]["containers"] if item["name"] == "probe"]
        if len(probe) != 1:
            raise SafeError("probe container contract missing")
        expected = {key.upper().replace("-", "_"): key for key in ("client-secret", "client-id", "generation")}
        actual = {item["name"]: item.get("valueFrom", {}).get("secretKeyRef", {}) for item in probe[0].get("env", []) if item["name"] in expected}
        if any(actual.get(key) != {"name": s["current_secret"], "key": value} for key, value in expected.items()):
            raise SafeError("deployment credential references differ from terminal state")
        self.uuid = self.kc.resolve(self.client)
        old = self.credential(s["current_secret"])
        if self.kc.secret(self.uuid) != old:
            self.block("server principal differs from distributed terminal generation; no recovery POST sent")
        metadata = self.kc.metadata(self.uuid)
        expiry = metadata.get(META_KEYS[1], 0)
        if expiry <= 0 or self.clock() <= expiry + self.epsilon:
            raise SafeError("current generation is not proven expired; use normal rotation while valid")
        rejected = self.auth(old, s["generation"], "expired_recovery_baseline")
        if rejected["ok"] or rejected["http"] not in {400, 401} or rejected["error"] not in EXPIRY_REJECTION_ERRORS:
            raise SafeError("expired recovery requires a fresh credential rejection, not network or administrative failure")
        next_generation = s["generation"] + 1
        self.save(phase="INTENT_RECORDED", recovery_mode="expired-current", preexisting_outage=True,
                  expired_verified_at=self.clock(), cycle_id=str(uuid.uuid4()), old_secret=s["current_secret"],
                  old_generation=s["generation"], next_generation=next_generation,
                  next_secret=f"catalogo-api-cred-v{next_generation:04d}", pre_metadata=metadata,
                  baseline_auth=rejected, intent_at=self.clock(), old_probe_rejected=False,
                  old_probe_result=None, old_retired_early=False, old_retirement_intent_at=None, old_retirement_verified_at=None, hold_skipped_reason=None, post_finished_at=None, rollout_started_at=None)
        self.run(reconcile_only=True)

    def run(self, manual=False, reconcile_only=False):
        s = self.state.value
        self.uuid = self.kc.resolve(self.client)
        if s["phase"] == "BLOCKED":
            raise SafeError("cycle is BLOCKED; inspect evidence and recover explicitly")
        if s["phase"] in TERMINAL:
            if reconcile_only:
                event("reconcile_no_pending_cycle")
                return
            if not manual and (s.get("suspended", True) or self.clock() < s.get("last_rotated_at", 0) + self.interval):
                event("decision_no_rotation", reason="suspended_or_not_due")
                return
            if not self.verify_pods(s["generation"]):
                raise SafeError("baseline pods are not healthy; no rotation sent")
            old = self.credential(s["current_secret"])
            if self.kc.secret(self.uuid) != old:
                self.block("RHBK principal differs from distributed current generation")
            baseline_auth = self.auth(old, s["generation"], "baseline")
            if not baseline_auth["ok"]:
                raise SafeError("baseline authentication failed; no rotation sent")
            meta = self.kc.metadata(self.uuid)
            if meta.get(META_KEYS[1], 0) <= self.clock() + self.pre_post_budget:
                raise SafeError("insufficient original validity budget for safe rotation")
            self.old_probe(s["generation"])
            next_gen = s["generation"] + 1
            self.save(phase="INTENT_RECORDED", recovery_mode=None, preexisting_outage=False, expired_verified_at=None, cycle_id=str(uuid.uuid4()), old_secret=s["current_secret"], old_generation=s["generation"], next_generation=next_gen, next_secret=f"catalogo-api-cred-v{next_gen:04d}", pre_metadata=meta, baseline_auth=baseline_auth, intent_at=self.clock(), old_probe_rejected=False, old_probe_result=None, old_retired_early=False, old_retirement_intent_at=None, old_retirement_verified_at=None, hold_skipped_reason=None, post_finished_at=None, rollout_started_at=None)
        if self.state.value["phase"] == "INTENT_RECORDED":
            # An intent alone means no POST has been sent. Recheck immediately
            # before marking the uncertain mutation boundary, including resume
            # after a crash while still in INTENT_RECORDED.
            if self.kc.secret(self.uuid) != self.credential(self.state.value["old_secret"]):
                self.block("principal changed after intent and before POST")
            current_metadata = self.kc.metadata(self.uuid)
            remaining = current_metadata.get(META_KEYS[1], 0) - self.clock()
            if self.state.value.get("recovery_mode") == "expired-current":
                expired = current_metadata.get(META_KEYS[1], 0)
                if expired <= 0 or self.clock() <= expired + self.epsilon:
                    self.block("expired recovery precondition changed before POST")
                rejected = self.auth(self.credential(self.state.value["old_secret"]), self.state.value["old_generation"], "expired_recovery_pre_post")
                if rejected["ok"] or rejected["http"] not in {400, 401} or rejected["error"] not in EXPIRY_REJECTION_ERRORS:
                    raise SafeError("expired credential rejection no longer proven; no recovery POST sent")
            elif remaining <= self.pre_post_budget:
                raise SafeError("insufficient original validity immediately before POST; intent remains unmutated")
            self.save(phase="ROTATING", post_started_at=self.clock(), pre_metadata=current_metadata,
                      original_validity_before_post=remaining)
            self.lease.check()
            new, interval = self.kc.rotate(self.uuid)
            event("rotation_response_received", http=interval.get("http", 200), generation=f"v{self.state.value['next_generation']:04d}", started_at=interval["started"], finished_at=interval["finished"])
            if os.getenv("INJECT_FAILURE") == "after-post":
                raise SafeError("injected crash after POST; reconcile reads authoritative credential")
            if new == self.credential(self.state.value["old_secret"]):
                self.block("POST did not produce a different credential")
            self.save(post_finished_at=interval["finished"])
        if self.state.value["phase"] in {"ROTATING", "RECONCILING", "FAILED"}:
            self.reconcile_rotation()
        s = self.state.value
        old, new = self.credential(s["old_secret"]), self.credential(s["next_secret"])
        if s["phase"] == "SECRET_STAGED":
            if s.get("recovery_mode") == "expired-current":
                self.save(phase="RETIRING_OLD", old_retirement_intent_at=self.clock())
            else:
                self.save(phase="HOLDING_OLD")
        if s["phase"] == "RETIRING_OLD":
            if s.get("recovery_mode") != "expired-current":
                self.block("retirement state is restricted to explicit expired recovery")
            self.retire_expired_previous(old, new)
        if s["phase"] == "HOLDING_OLD":
            def hold_sample():
                result = self.auth(old, s["old_generation"], "old_during_hold")
                if not result["ok"]:
                    self.block("old credential failed during planned hold")
            if s["post_metadata"][META_KEYS[3]] + self.epsilon < max(s["hold_until"], self.clock()) + self.rollout_budget + self.margin:
                self.block("insufficient measured overlap remaining; review recovery before rollout")
            self.wait_until(s["hold_until"], hold_sample)
            self.save(phase="ROLLING_OUT", rollout_started_at=self.clock())
        if s["phase"] == "ROLLING_OUT":
            if not self.auth(new, s["next_generation"], "before_rollout")["ok"]:
                self.block("staged generation no longer authenticates; refusing rollout")
            # Idempotent PUT preserves the recorded generation; never creates another secret.
            self.start_rollout()
            deadline = self.clock() + self.rollout_budget
            while not self.verify_pods(s["next_generation"]):
                if self.clock() >= deadline:
                    raise SafeError("rollout incomplete; reconcile same staged generation")
                self.lease.check()
                self.sleep(2)
            self.save(phase="VERIFYING", rollout_completed_at=self.clock())
        if s["phase"] == "VERIFYING":
            if not self.auth(new, s["next_generation"], "main")["ok"]:
                self.block("new generation failed authentication")
            retired = s.get("recovery_mode") == "expired-current" and s.get("old_retired_early") is True
            if not retired:
                old_expiry = min(s["post_metadata"][META_KEYS[3]], s["post_metadata"][META_KEYS[1]])
                self.wait_until(old_expiry + self.epsilon)
            old_result = self.auth(old, s["old_generation"], "old_after_invalidation" if retired else "old_after_expiry")
            if old_result["ok"]:
                self.block("old credential still authenticates after recorded expiry or explicit invalidation")
            # RHBK 26.6.9 returned unauthorized_client for an expired rotated
            # credential. Normal cycles had a successful S1 baseline; explicit expired
            # recovery separately records preexisting rejection/outage. In both
            # paths the measured deadline and positive S2 control are mandatory.
            if old_result["error"] not in EXPIRY_REJECTION_ERRORS or old_result["http"] not in {400, 401}:
                raise SafeError("old credential expiry inconclusive: network/TLS/server failure is not expiry")
            if not self.auth(new, s["next_generation"], "main_after_old_expiry")["ok"] or not self.verify_pods(s["next_generation"]):
                self.block("main generation unhealthy after old expiry")
            history = s.get("history", [])[-19:] + [{"cycle_id": s["cycle_id"], "generation": s["next_generation"], "post_started_at": s["post_started_at"], "post_finished_at": s["post_finished_at"], "rollout_completed_at": s["rollout_completed_at"], "completed_at": self.clock(), "old_rejected": True, "old_rejection_oauth": old_result["error"], "recovery_mode": s.get("recovery_mode"), "preexisting_outage": s.get("preexisting_outage", False), "old_rejection_basis": "explicit-invalidation" if retired else "natural-expiration", "old_retired_early": retired, "pre_metadata": s["pre_metadata"], "post_metadata": s["post_metadata"]}]
            self.save(phase="COMPLETE", generation=s["next_generation"], current_secret=s["next_secret"], last_rotated_at=s["post_finished_at"] or s["post_started_at"], completed_at=self.clock(), old_probe_rejected=True, old_probe_result=old_result, history=history)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["evaluate", "rotate-once", "recover-expired", "reconcile", "status", "suspend", "resume"])
    args = parser.parse_args()
    lease = None
    try:
        kube = Kube()
        if args.command == "status":
            state = State(kube, os.getenv("STATE_NAME", "rotation-state"))
            event("status", **state.value)
            return 0
        lease = Lease(kube, os.getenv("LEASE_NAME", "rotation-catalogo-api")).acquire()
        state = State(kube, os.getenv("STATE_NAME", "rotation-state"))
        if args.command in {"suspend", "resume"}:
            suspended = args.command == "suspend"
            if not suspended and state.value["phase"] == "BLOCKED":
                raise SafeError("cannot resume a blocked cycle")
            path = kube.batch + "/cronjobs/rotation-controller"
            cron = kube.get(path)
            kube.owned(cron)
            cron["spec"]["suspend"] = suspended
            lease.check()
            kube.replace(path, cron)
            state.save(suspended=suspended)
            event("automation", suspended=suspended)
        else:
            controller = Controller(kube, Keycloak(), state, lease)
            if args.command == "recover-expired":
                controller.recover_expired()
            else:
                controller.run(manual=args.command == "rotate-once", reconcile_only=args.command == "reconcile")
        return 0
    except SafeError as exc:
        event("controller_error", reason=str(exc), recovery="inspect state, then reconcile; never blindly repeat POST")
        return 2
    except (KeyError, ValueError, OSError, TypeError):
        event("controller_error", reason="configuration or persisted-state validation failed")
        return 2
    finally:
        if lease:
            lease.close()


if __name__ == "__main__":
    sys.exit(main())
