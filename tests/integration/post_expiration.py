#!/usr/bin/env python3
"""Real CRC/RHBK experiments; never substitute a mock for integrated evidence.

Secrets/token response bodies stay in memory. All evidence is constructed from
an explicit metadata allowlist. Mutation responses are never retried.
"""
import argparse
import csv
import datetime as dt
import email.utils
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
METADATA = {
    "created": "client.secret.creation.time",
    "expires": "client.secret.expiration.time",
    "rotated_created": "client.secret.rotated.creation.time",
    "rotated_expires": "client.secret.rotated.expiration.time",
}
OAUTH_ERRORS = {"invalid_client", "unauthorized_client", "invalid_grant",
                "invalid_request", "invalid_scope", "server_error",
                "temporarily_unavailable", "unsupported_grant_type"}
OWNER = "client-secret-rotation"
NAMESPACE = "rhbk-rotation-lab"
ROLE = "lab-secret-rotation"


class Blocked(Exception):
    """An unmet prerequisite or measurement ambiguity, without sensitive data."""


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                    dt.timezone.utc).isoformat(timespec="milliseconds")


def epoch_seconds(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise Blocked("metadata_timestamp_not_numeric") from None
    if not math.isfinite(value) or value < 0 or value > 1e15:
        raise Blocked("metadata_timestamp_out_of_range")
    return value / 1000 if value > 1e11 else value


def metadata(client):
    attrs = client.get("attributes", {})
    return {key: epoch_seconds(attrs.get(name)) for key, name in METADATA.items()}


def oauth_error(body):
    error = body.get("error") if isinstance(body, dict) else None
    return error if error in OAUTH_ERRORS else ("other_oauth_error" if error else None)


def valid_auth(result):
    return result.get("http") == 200 and result.get("token_received") is True


def invalid_auth(result):
    """Credential rejection; callers must also supply timed/positive controls.

    RHBK 26.6.9 uses unauthorized_client for an expired secret. This predicate
    alone is not proof of expiry; baseline, immutable credential and deadlines
    (plus the current-secret control where applicable) establish that context.
    """
    return result.get("http") in (400, 401) and result.get("oauth_error") in {"invalid_client", "unauthorized_client"}


def classify_error(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    return "network_error"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, ca_file, timeout=10):
        self.context = ssl.create_default_context(cafile=ca_file)
        self.timeout = timeout
        self.clock_samples = []

    def request(self, method, url, body=None, token=None, form=False):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            headers["Content-Type"] = ("application/x-www-form-urlencoded" if form
                                       else "application/json")
            data = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
        start, mono = time.time(), time.monotonic()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.context), NoRedirect())
            with opener.open(req, timeout=self.timeout) as r:
                code, raw, date = r.status, r.read(), r.headers.get("Date")
        except urllib.error.HTTPError as exc:
            code, raw, date = exc.code, exc.read(), exc.headers.get("Date")
            exc.close()
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as exc:
            return 0, {}, {"category": classify_error(exc), "start": start,
                           "end": time.time(), "latency_ms": round((time.monotonic()-mono)*1000, 2)}
        end = time.time()
        if abs((end-start)-(time.monotonic()-mono)) > 2:
            raise Blocked("host_clock_jump_during_request")
        info = {"category": "http", "start": start, "end": end,
                "latency_ms": round((time.monotonic()-mono)*1000, 2)}
        if date:
            try:
                server = email.utils.parsedate_to_datetime(date).timestamp()
                info["server_date"] = server
                info["server_offset_seconds"] = round(server-(start+end)/2, 3)
                self.clock_samples.append({key: info[key] for key in
                                          ("start", "end", "server_date", "server_offset_seconds")})
            except (TypeError, ValueError, OverflowError):
                pass
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return code, parsed, info


class API:
    def __init__(self, url, ca_file, realm, admin_id, secret_file):
        self.url, self.realm, self.admin_id = url.rstrip("/"), realm, admin_id
        self.secret_file = Path(secret_file)
        self.http = Transport(ca_file)
        self.token, self.token_until = None, 0
        self.base = "/admin/realms/" + urllib.parse.quote(realm, safe="")
        self.token_path = "/realms/" + urllib.parse.quote(realm, safe="") + "/protocol/openid-connect/token"

    def access(self):
        if time.monotonic() < self.token_until:
            return self.token
        secret = self.secret_file.read_text().strip()
        code, body, info = self.http.request("POST", self.url+self.token_path, {
            "grant_type": "client_credentials", "client_id": self.admin_id,
            "client_secret": secret}, form=True)
        if code != 200 or not body.get("access_token"):
            raise Blocked("independent_admin_auth_failed_" + str(code) + "_" + info["category"])
        self.token = body["access_token"]
        self.token_until = time.monotonic()+max(1, int(body.get("expires_in", 30))-15)
        return self.token

    def admin(self, method, path, body=None, expected=(200, 201, 204)):
        code, result, info = self.http.request(method, self.url+self.base+path,
                                               body, self.access())
        if code not in expected:
            # No retry: a lost POST may already have rotated the secret.
            suffix = "_mutation_outcome_unknown_no_retry" if method in ("POST", "PUT", "DELETE") and not code else ""
            raise Blocked("admin_" + method + "_" + str(code) + "_" + info["category"] + suffix)
        return result, info

    def resolve(self, client_id):
        values, _ = self.admin("GET", "/clients?"+urllib.parse.urlencode({"clientId": client_id}))
        exact = [v for v in values if v.get("clientId") == client_id]
        if len(exact) != 1:
            raise Blocked("client_uuid_not_unique")
        return exact[0]["id"]

    def get_meta(self, cid):
        body, _ = self.admin("GET", "/clients/"+cid)
        return metadata(body)

    def secret(self, cid):
        body, _ = self.admin("GET", "/clients/"+cid+"/client-secret")
        if not isinstance(body.get("value"), str) or not body["value"]:
            raise Blocked("secret_readback_unavailable")
        return body["value"]

    def rotate(self, cid):
        body, info = self.admin("POST", "/clients/"+cid+"/client-secret", {})
        if not isinstance(body.get("value"), str) or not body["value"]:
            raise Blocked("rotation_response_missing_secret_no_retry")
        return body["value"], info

    def auth(self, client_id, secret, keep_token=False):
        code, body, info = self.http.request("POST", self.url+self.token_path, {
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": secret}, form=True)
        result = {"http": code, "oauth_error": oauth_error(body),
                  "category": info["category"], "token_received": bool(body.get("access_token")),
                  "latency_ms": info["latency_ms"], "timestamp": utc(info["end"]),
                  "epoch": info["end"]}
        if keep_token:
            return result, body.get("access_token")
        return result

    def create(self, scenario, policy=True):
        client_id = "expiry-"+scenario+"-"+uuid.uuid4().hex[:10]
        self.admin("POST", "/clients", {"clientId": client_id, "enabled": True,
            "protocol": "openid-connect", "publicClient": False,
            "clientAuthenticatorType": "client-secret", "serviceAccountsEnabled": True,
            "standardFlowEnabled": False, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "attributes": {"lab.owner": OWNER, "lab.experiment": scenario}})
        cid = self.resolve(client_id)
        if policy:
            self.admin("POST", "/clients/"+cid+"/roles", {"name": ROLE,
                "description": "Marker for isolated client-secret-rotation laboratory"})
            # Initial secret is defined only after the marker role exists.
            s1, interval = self.rotate(cid)
        else:
            s1 = self.secret(cid)
            interval = {"start": time.time(), "end": time.time()}
        return client_id, cid, s1, interval, self.get_meta(cid)


def oc_raw(args):
    kubeconfig = os.environ.get("KUBECONFIG")
    if not kubeconfig or os.pathsep in kubeconfig:
        raise Blocked("dedicated_crc_KUBECONFIG_required")
    try:
        result = subprocess.run(["oc", "--kubeconfig", kubeconfig, "--request-timeout=10s", *args],
                                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("crc_oc_unavailable") from None
    if result.returncode:
        raise Blocked("crc_read_only_guard_failed")
    return result.stdout


def oc(args):
    try:
        return json.loads(oc_raw(args))
    except json.JSONDecodeError:
        raise Blocked("crc_guard_invalid_json") from None


def pod_clock_sample(runtime_version, epsilon=5):
    """Read the OS clock in the verified RHBK pod, without writing cluster state.

    Operator-created Pods do not inherit the CR ownership label. Verify the
    Pod -> StatefulSet -> owned Keycloak CR UID chain instead of trusting names.
    The pod OS clock is the underlying JVM wall-clock source, not an HTTP Date.
    """
    if not re.fullmatch(r"26\.6\.\d+(?:[.-].*)?", runtime_version):
        raise Blocked("RHBK_runtime_version_required_for_pod_clock")
    namespace = oc(["get", "namespace", NAMESPACE, "-o", "json"])
    if namespace.get("metadata", {}).get("labels", {}).get("lab.rhbk/owner") != OWNER:
        raise Blocked("pod_clock_namespace_not_owned")
    pod = oc(["-n", NAMESPACE, "get", "pod", "rhbk-0", "-o", "json"])
    statefulset = oc(["-n", NAMESPACE, "get", "statefulset", "rhbk", "-o", "json"])
    keycloak = oc(["-n", NAMESPACE, "get", "keycloak", "rhbk", "-o", "json"])
    if keycloak.get("metadata", {}).get("labels", {}).get("lab.rhbk/owner") != OWNER:
        raise Blocked("pod_clock_Keycloak_not_owned")
    def owned_by(resource, parent, kind):
        return any(ref.get("kind") == kind and ref.get("name") == parent["metadata"]["name"]
                   and ref.get("uid") == parent["metadata"]["uid"]
                   for ref in resource.get("metadata", {}).get("ownerReferences", []))
    if not owned_by(pod, statefulset, "StatefulSet") or not owned_by(statefulset, keycloak, "Keycloak"):
        raise Blocked("pod_clock_owner_chain_not_verified")
    containers = [item for item in pod.get("spec", {}).get("containers", []) if item.get("name") == "keycloak"]
    image = keycloak.get("spec", {}).get("image", "")
    if len(containers) != 1 or containers[0].get("image") != image or "@sha256:" not in image:
        raise Blocked("pod_clock_image_not_bound_to_verified_Keycloak_CR")
    start, mono = time.time(), time.monotonic()
    raw = oc_raw(["-n", NAMESPACE, "exec", "rhbk-0", "-c", "keycloak", "--", "date", "-u", "+%s"]).strip()
    end, elapsed = time.time(), time.monotonic()-mono
    if not re.fullmatch(r"\d{10,11}", raw):
        raise Blocked("RHBK_pod_clock_invalid_response")
    if abs((end-start)-elapsed) > epsilon:
        raise Blocked("host_clock_jump_during_pod_clock_measurement")
    server = int(raw)
    lower, upper = server-end, server+1-start
    sample = {"source": "RHBK_pod_OS_clock", "pod": "rhbk-0", "container": "keycloak",
              "pod_uid": pod["metadata"]["uid"], "runtime_version": runtime_version,
              "host_start": start, "host_end": end, "server_epoch_seconds": server,
              "server_offset_seconds": round((lower+upper)/2, 3),
              "offset_lower_seconds": round(lower, 3), "offset_upper_seconds": round(upper, 3),
              "measurement_duration_seconds": round(elapsed, 3),
              "precision_seconds": 1, "ownership_chain_verified": True}
    if lower < -epsilon or upper > epsilon:
        raise Blocked("host_RHBK_pod_clock_offset_or_uncertainty_exceeds_epsilon")
    return sample


def guard(url, realm):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".apps-crc.testing"):
        raise Blocked("RHBK_URL_must_be_verified_CRC_HTTPS_route")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Blocked("RHBK_URL_contains_unexpected_components")
    if realm != "lab-secret-rotation":
        raise Blocked("isolated_lab_realm_required")
    context = oc(["config", "view", "--minify", "-o", "json"])
    servers = [v["cluster"]["server"] for v in context.get("clusters", [])]
    if servers != ["https://api.crc.testing:6443"]:
        raise Blocked("cluster_is_not_CRC")
    try:
        identity = json.loads((ROOT/"evidence/cluster-identity.json").read_text())
    except (OSError, ValueError):
        raise Blocked("recorded_CRC_identity_unavailable") from None
    if not isinstance(identity, dict) or identity.get("server") != servers[0] or not isinstance(identity.get("uid"), str):
        raise Blocked("recorded_CRC_identity_invalid")
    cluster = oc(["get", "namespace", "kube-system", "-o", "json"])
    if not identity.get("uid") or cluster.get("metadata", {}).get("uid") != identity["uid"]:
        raise Blocked("CRC_cluster_UID_changed")
    namespace = oc(["get", "namespace", NAMESPACE, "-o", "json"])
    if namespace.get("metadata", {}).get("labels", {}).get("lab.rhbk/owner") != OWNER:
        raise Blocked("namespace_ownership_not_verified")
    try:
        namespace_identity = json.loads((ROOT/"evidence/namespace-identity.json").read_text())
    except (OSError, ValueError):
        raise Blocked("recorded_namespace_identity_missing_run_setup") from None
    if (not isinstance(namespace_identity, dict) or namespace_identity.get("name") != NAMESPACE
            or namespace_identity.get("cluster_uid") != identity["uid"]
            or namespace_identity.get("uid") != namespace.get("metadata", {}).get("uid")
            or not namespace_identity.get("uid")):
        raise Blocked("namespace_was_not_created_by_this_checkout")
    cron = oc(["-n", NAMESPACE, "get", "cronjob", "rotation-controller", "-o", "json"])
    if cron.get("spec", {}).get("suspend") is not True:
        raise Blocked("suspend_lab_cronjob_before_experiments")
    jobs = oc(["-n", NAMESPACE, "get", "jobs", "-l", "app.kubernetes.io/name=rotation-controller", "-o", "json"])
    if any(job.get("status", {}).get("active", 0) for job in jobs.get("items", [])):
        raise Blocked("active_controller_job_during_experiment")
    return {"api": servers[0], "cluster_uid": identity["uid"], "namespace_uid": namespace["metadata"]["uid"],
            "cronjob_suspended": True, "active_controller_jobs": 0}


class Evidence:
    FIELDS = ["build", "scenario", "event", "timestamp", "generation", "created",
              "original_expiration", "expiration_after_post", "rotated_expiration",
              "expected_delta", "observed_delta", "http", "oauth_error", "category", "result"]

    def __init__(self, directory, build):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.build, self.events, self.results = build, [], []
        self.path = self.directory / ("events-"+dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")+".jsonl")

    def add(self, scenario, event, **values):
        item = {"build": self.build, "scenario": scenario, "event": event, "timestamp": utc(), **values}
        # Callers pass only explicit metadata and observations, never HTTP bodies.
        self.events.append(item)
        with self.path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(item, sort_keys=True)+"\n")
        print(json.dumps(item, sort_keys=True), flush=True)
        return item

    def finish(self):
        with self.path.with_suffix(".csv").open("w", newline="", encoding="utf8") as stream:
            writer = csv.DictWriter(stream, fieldnames=self.FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.events)
        summary = {"build": self.build, "finished_at": utc(), "results": self.results,
                   "evidence": self.path.name, "secrets_or_tokens_recorded": False,
                   "integrated_execution": bool(any(e.get("event") == "S1_initialized" for e in self.events))}
        (self.directory/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        lines = ["# Medição de expiração após POST", "", "Build efetiva: `"+self.build+"`.", "",
                 "Este relatório descreve somente os eventos realmente registrados nesta execução.", "",
                 "| Cenário | Estado | Conclusão / bloqueio |", "|---|---|---|"]
        for result in self.results:
            note = result.get("reason", result.get("post_renewal", result.get("note", "Ver eventos medidos")))
            lines.append("| "+result["scenario"]+" | "+result["result"]+" | "+str(note)+" |")
        lines += ["", "| Evento | UTC | Geração | Expiração original | Expiração após POST | Expiração anterior | HTTP | OAuth | Estado |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for item in self.events:
            if item.get("event") == "clock_samples_final":
                continue
            values = [str(item.get(key, "")) for key in ("event", "timestamp", "generation", "original_expiration",
                      "expiration_after_post", "rotated_expiration", "http", "oauth_error", "result")]
            lines.append("| "+" | ".join(values)+" |")
        (self.directory/"report.md").write_text("\n".join(lines)+"\n")
        return summary


class Experiment:
    def __init__(self, api, evidence, ttl=300, rotated=120, remaining=60, epsilon=5):
        self.api, self.ev = api, evidence
        self.ttl, self.rotated, self.remaining, self.eps = ttl, rotated, remaining, epsilon
        if not 0 < rotated < ttl or not 0 < remaining < ttl or epsilon < 1 or ttl > 3600:
            raise Blocked("invalid_experiment_timing")
        self.initial_clock = (time.time(), time.monotonic())
        self.clock_checker = None
        self.last_clock_check = time.monotonic()

    def verify_profile(self):
        """Refuse a long-lived or mismatched profile before creating test clients."""
        model, _ = self.api.admin('GET', '/client-policies/profiles')
        profiles = [item for item in model.get('profiles', [])
                    if item.get('name') == 'lab-secret-rotation-profile']
        if len(profiles) != 1:
            raise Blocked('short_experiment_profile_missing_or_ambiguous')
        executors = [item for item in profiles[0].get('executors', [])
                     if item.get('executor') == 'secret-rotation']
        expected = {'expiration-period': self.ttl, 'rotated-expiration-period': self.rotated,
                    'remaining-rotation-period': self.remaining}
        if len(executors) != 1 or any(executors[0].get('configuration', {}).get(k) != v
                                      for k, v in expected.items()):
            raise Blocked('short_experiment_profile_mismatch_do_not_wait_for_long_lived_expiration')

    def wait(self, deadline):
        if deadline-time.time() > 2*self.ttl+self.rotated+2*self.eps+60:
            raise Blocked('server_deadline_exceeds_bounded_experiment_budget')
        next_heartbeat = time.monotonic()+30
        while time.time() < deadline:
            wall, mono = self.initial_clock
            if abs((time.time()-wall)-(time.monotonic()-mono)) > self.eps:
                raise Blocked("host_clock_jump_or_VM_pause")
            if self.clock_checker and time.monotonic()-self.last_clock_check >= 30:
                sample = self.clock_checker()
                self.ev.add("clock", "periodic_RHBK_pod_clock_comparison", result="PASS", **sample)
                self.last_clock_check = time.monotonic()
            if time.monotonic() >= next_heartbeat:
                print(json.dumps({"event": "waiting_for_measurement", "timestamp": utc(),
                                  "seconds_remaining": round(deadline-time.time(), 1)}), flush=True)
                next_heartbeat = time.monotonic()+30
            time.sleep(min(1, max(0, deadline-time.time())))

    def check(self, name, condition, **values):
        result = "PASS" if condition else "FAIL"
        self.ev.add(self.scenario, name, result=result, **values)
        self.checks.append(condition)
        return condition

    def auth(self, event, generation, secret, expected=None):
        result = self.api.auth(self.client_id, secret)
        condition = None
        if expected == "valid":
            condition = valid_auth(result)
        elif expected == "invalid":
            condition = invalid_auth(result)
        self.ev.add(self.scenario, event, generation=generation,
                    result="OBSERVED" if condition is None else ("PASS" if condition else "FAIL"),
                    **result)
        if result["http"] == 0 or result["http"] >= 500:
            raise Blocked("authentication_"+result["category"]+"_is_not_expiration_evidence")
        if condition is not None:
            self.checks.append(condition)
        return result

    def setup(self, scenario, policy=True, baseline=True):
        self.scenario, self.checks = scenario, []
        self.client_id, self.cid, self.s1, self.init, self.before = self.api.create(scenario, policy)
        self.t0 = self.before["created"] or self.init["end"]
        self.e1 = self.before["expires"]
        self.ev.add(scenario, "S1_initialized", generation="S1", client_id=self.client_id,
                    profile="lab-secret-rotation-profile" if policy else "none",
                    created=self.before["created"], original_expiration=self.e1,
                    initialization_start=self.init["start"], initialization_end=self.init["end"],
                    ttl_principal=self.ttl, ttl_rotated=self.rotated, epsilon=self.eps)
        if baseline:
            self.auth("baseline", "S1", self.s1, "valid")
        if policy and self.e1 is None:
            self.ev.add(scenario, "metadata_not_exposed", result="NOT_MEASURED",
                        fallback="timed_new_authentications")
        elif policy:
            self.check("initial_TTL", self.init["start"]+self.ttl-self.eps <= self.e1 <= self.init["end"]+self.ttl+self.eps,
                       observed_delta=self.e1-self.t0, expected_delta=self.ttl)
        self.original_deadline = self.e1 or self.init["end"]+self.ttl

    def post(self):
        self.s2, self.post_info = self.api.rotate(self.cid)
        self.after = self.api.get_meta(self.cid)
        self.e2, self.old_exp = self.after["expires"], self.after["rotated_expires"]
        self.check("POST_changed_value_in_memory", self.s1 != self.s2, generation="S2")
        self.ev.add(self.scenario, "POST_metadata", generation="S2", created=self.after["created"],
                    original_expiration=self.e1, expiration_after_post=self.e2,
                    rotated_expiration=self.old_exp,
                    post_start=self.post_info["start"], post_end=self.post_info["end"],
                    expected_delta=self.ttl,
                    observed_delta=self.e2-self.post_info["end"] if self.e2 else None)
        if self.e2 is not None:
            self.check("E2_matches_POST_plus_TTL", self.post_info["start"]+self.ttl-self.eps <= self.e2 <= self.post_info["end"]+self.ttl+self.eps)
            if self.e1 is not None and self.scenario in {"early", "near", "expired"}:
                self.check("E2_advanced_over_E1_original", self.e2 > self.e1,
                           observed_delta=self.e2-self.e1)
        self.new_deadline = self.e2 or self.post_info["end"]+self.ttl
        self.old_deadline = self.old_exp or self.post_info["end"]+self.rotated
        self.auth("S2_immediately_after_POST", "S2", self.s2, "valid")

    def end(self, **extra):
        result = "PASS" if self.checks and all(self.checks) else "FAIL"
        item = {"scenario": self.scenario, "result": result, "checks": len(self.checks), **extra}
        self.ev.results.append(item)
        self.ev.add(self.scenario, "conclusion", result=result, checks=len(self.checks), **extra)
        return item

    def expiration(self, mode):
        self.setup(mode)
        if mode == "early":
            self.wait(self.init["end"]+90)
            if self.original_deadline-time.time() <= self.remaining+self.eps:
                raise Blocked("POST_not_outside_dynamic_update_window")
        elif mode == "near":
            self.wait(self.original_deadline-30)
        elif mode == "expired":
            self.wait(self.original_deadline+self.eps+1)
            self.auth("S1_expired_before_POST", "S1", self.s1, "invalid")
            unchanged = self.api.secret(self.cid) == self.s1
            self.check("no_rotation_due_to_time_alone", unchanged)
        self.post()
        # The server, not the test, determines whether an expired old secret revives.
        self.auth("S1_immediately_after_POST", "S1", self.s1,
                  "valid" if mode != "expired" else None)
        if mode == "early" and self.old_exp is not None:
            self.check("old_expiration_matches_rotation_window",
                       self.post_info["start"]+self.rotated-self.eps <= self.old_exp <= self.post_info["end"]+self.rotated+self.eps)
        if self.old_exp is not None:
            self.ev.add(mode, "old_secret_expiration_rule", original_expiration=self.e1,
                        rotated_expiration=self.old_exp,
                        result="rotation_plus_TTL" if abs(self.old_exp-self.post_info["end"]-self.rotated) <= self.eps else
                        "limited_by_original" if self.e1 and abs(self.old_exp-self.e1) <= self.eps else "other_observed_rule")
        # Schedule probes on both sides of original, rotated, and new expiration.
        events = [
            (self.old_deadline-self.eps-1, "S1_before_rotated_expiration", "S1", self.s1, "valid" if mode != "expired" else None),
            (self.old_deadline+self.eps+1, "S1_after_rotated_expiration", "S1", self.s1, "invalid"),
            (self.old_deadline+self.eps+1, "S2_after_old_expiration", "S2", self.s2, "valid"),
            (self.original_deadline-self.eps-1, "S1_before_original_expiration", "S1", self.s1, None),
            (self.original_deadline+self.eps+1, "S1_after_original_expiration", "S1", self.s1, None),
            (self.original_deadline+self.eps+1, "S2_beyond_original_expiration", "S2", self.s2, "valid"),
            (self.new_deadline-self.eps-1, "S2_before_own_expiration", "S2", self.s2, "valid"),
            (self.new_deadline+self.eps+1, "S2_after_own_expiration", "S2", self.s2, "invalid"),
        ]
        for deadline, event, gen, secret, expected in sorted(events):
            if deadline <= self.post_info["end"]:
                continue
            if event == "S2_beyond_original_expiration" and deadline >= self.new_deadline-self.eps:
                self.check("window_after_E1_before_E2_exists", False)
                continue
            self.wait(deadline)
            self.auth(event, gen, secret, expected)
        self.check("no_additional_secret_mutations", self.api.secret(self.cid) == self.s2)
        return self.end(metadata_exposed=self.e1 is not None and self.e2 is not None,
                        post_renewal="supported_by_measurements" if all(self.checks) else "not_confirmed")

    def control(self):
        self.setup("control", False)
        self.wait(self.init["end"]+2)
        self.post()
        self.check("control_no_expiration_assigned", self.e1 is None and self.e2 is None)
        self.auth("S1_after_POST_without_policy", "S1", self.s1, "invalid")
        self.wait(self.post_info["end"]+self.ttl+self.eps+1)
        self.auth("S2_beyond_policy_TTL_without_policy", "S2", self.s2, "valid")
        return self.end(note="No expiration is imputed to the control client")

    def no_automation(self):
        self.setup("no-automation")
        self.wait(self.original_deadline-self.eps-1)
        self.auth("S1_before_expiration", "S1", self.s1, "valid")
        self.wait(self.original_deadline+self.eps+1)
        self.auth("S1_after_expiration", "S1", self.s1, "invalid")
        self.check("time_alone_did_not_rotate", self.api.secret(self.cid) == self.s1)
        return self.end()

    def double(self):
        self.setup("double")
        self.post()
        s3, _ = self.api.rotate(self.cid)
        self.check("three_distinct_values_in_memory", len({self.s1, self.s2, s3}) == 3)
        self.auth("S1_after_double_rotation", "S1", self.s1, "invalid")
        self.auth("S2_after_double_rotation", "S2", self.s2, "valid")
        self.auth("S3_after_double_rotation", "S3", s3, "valid")
        return self.end()

    def invalidate(self):
        self.setup("invalidate")
        self.post()
        self.auth("S1_before_DELETE", "S1", self.s1, "valid")
        self.api.admin("DELETE", "/clients/"+self.cid+"/client-secret/rotated")
        self.auth("S1_after_DELETE", "S1", self.s1, "invalid")
        self.auth("S2_after_DELETE", "S2", self.s2, "valid")
        return self.end()

    def permissions(self):
        self.setup("permissions", False)
        result, token = self.api.auth(self.client_id, self.s1, keep_token=True)
        if not valid_auth(result):
            raise Blocked("permissions_baseline_failed")
        status, _, info = self.api.http.request("GET", self.api.url+self.api.base+"/clients/"+self.cid, token=token)
        self.check("M2M_identity_denied_admin_API", status in (401, 403), http=status, category=info["category"])
        discovery = self.api.url+"/realms/"+self.api.realm+"/.well-known/openid-configuration"
        code, _, info = self.api.http.request("GET", discovery)
        if code != 200:
            raise Blocked("TLS_control_with_correct_CA_unavailable")
        self.check("correct_CA_and_hostname_accepted", code == 200, http=code)
        # Empty trust store is deliberate. No credential is sent by these TLS probes.
        untrusted = Transport.__new__(Transport)
        untrusted.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        untrusted.timeout, untrusted.clock_samples = 10, []
        code, _, info = untrusted.request("GET", discovery)
        if code == 0 and info["category"] != "tls_error":
            raise Blocked("network_failure_cannot_prove_CA_validation")
        self.check("untrusted_CA_refused", code == 0 and info["category"] == "tls_error", http=code, category=info["category"])
        target = urllib.parse.urlsplit(self.api.url)
        refused = False
        try:
            with socket.create_connection((target.hostname, target.port or 443), timeout=10) as sock:
                with self.api.http.context.wrap_socket(sock, server_hostname="wrong-hostname.rhbk.invalid"):
                    pass
        except ssl.SSLCertVerificationError as exc:
            # OpenSSL X509_V_ERR_HOSTNAME_MISMATCH. No server error text is logged.
            refused = getattr(exc, "verify_code", None) == 62
        except (OSError, ssl.SSLError):
            raise Blocked("hostname_TLS_measurement_unavailable") from None
        self.check("incorrect_hostname_refused", refused)
        return self.end()

    def token_vs_secret(self):
        # This one disposable client needs its own audience for token introspection.
        # Do this before issuing the retained token; the normal M2M client is untouched.
        self.setup("token-vs-secret", baseline=False)
        mapper = {"name": "lab-self-audience", "protocol": "openid-connect",
                  "protocolMapper": "oidc-audience-mapper", "consentRequired": False,
                  "config": {"included.client.audience": self.client_id,
                             "access.token.claim": "true", "id.token.claim": "false"}}
        mapper_path = "/clients/"+self.cid+"/protocol-mappers/models"
        try:
            self.api.admin("POST", mapper_path, mapper)
        except Blocked as exc:
            # Only a definitive authorization refusal permits the setup identity.
            # TLS, network, timeout, 401 and unknown mutation outcomes never escalate.
            if str(exc) != "admin_POST_403_http":
                raise
            import importlib.util
            helper_spec = importlib.util.spec_from_file_location(
                "t15_mapper_setup", Path(__file__).with_name("mapper_setup.py"))
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
            try:
                receipt = helper.configure(self.api, self.cid, self.client_id, oc_raw, trigger_status=403)
            except helper.SetupBlocked as setup_error:
                raise Blocked(str(setup_error)) from None
            self.ev.add(self.scenario, "self_audience_mapper_bootstrap_setup", result="PASS", **receipt)
        else:
            mappers, _ = self.api.admin("GET", mapper_path)
            configured = [item for item in mappers if item.get("name") == mapper["name"]]
            if len(configured) != 1 or configured[0].get("protocolMapper") != mapper["protocolMapper"] or any(
                    configured[0].get("config", {}).get(key) != value for key, value in mapper["config"].items()):
                raise Blocked("T15_self_audience_mapper_not_verified")
        self.ev.add(self.scenario, "self_audience_mapper_verified", result="PASS", client_id=self.client_id)
        self.check("mapper_did_not_rotate_secret", self.api.secret(self.cid) == self.s1)
        result, issued_token = self.api.auth(self.client_id, self.s1, keep_token=True)
        self.ev.add(self.scenario, "retained_token_issued_using_S1", generation="S1", **result)
        if not valid_auth(result):
            raise Blocked("issued_token_baseline_failed")

        def introspect(event, credential):
            code, body, info = self.api.http.request("POST", self.api.url+self.api.token_path+"/introspect", {
                "client_id": self.client_id, "client_secret": credential, "token": issued_token}, form=True)
            active = body.get("active") is True
            expires_at = epoch_seconds(body.get("exp"))
            self.ev.add(self.scenario, event, http=code, active=active, token_expires_at=expires_at,
                        category=info["category"], result="PASS" if code == 200 and active else "FAIL",
                        note="Same retained token; active state is measured by introspection")
            if code == 0 or code >= 500:
                raise Blocked("T15_introspection_transport_failure")
            self.check(event+"_active", code == 200 and active, http=code, active=active)
            return active, expires_at

        active, token_expiry = introspect("baseline_token_introspection", self.s1)
        if not active:
            return self.end(note="Retained token was not active at baseline; no continuity conclusion")
        self.post()
        if token_expiry is not None:
            self.check("retained_token_TTL_outlives_old_secret", token_expiry > self.old_deadline+self.eps+1,
                       token_expires_at=token_expiry, rotated_expiration=self.old_deadline)
        self.wait(self.old_deadline+self.eps+1)
        self.auth("new_auth_with_expired_S1", "S1", self.s1, "invalid")
        self.auth("new_auth_with_current_S2", "S2", self.s2, "valid")
        introspect("previously_issued_token_after_S1_expiration", self.s2)
        return self.end(note="New S1 authentication rejected while its earlier token remained active" if all(self.checks) else "Token/secret distinction not confirmed; inspect failed checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["all", "early", "near", "expired", "control", "no-automation", "double", "invalidate", "permissions", "token-vs-secret"], default="all")
    parser.add_argument("--output", default="evidence/post-expiration")
    args = parser.parse_args()
    build = os.environ.get("RHBK_SERVER_VERSION", "NOT_MEASURED")
    ev = Evidence(args.output, build)
    scenarios = ["early", "near", "expired", "control"] if args.scenario == "all" else [args.scenario]
    try:
        needed = ["RHBK_URL", "RHBK_CA_FILE", "ADMIN_CLIENT_SECRET_FILE"]
        if any(not os.environ.get(k) for k in needed):
            raise Blocked("missing_runtime_configuration_"+"_".join(k for k in needed if not os.environ.get(k)))
        realm = os.environ.get("REALM", "lab-secret-rotation")
        info = guard(os.environ["RHBK_URL"], realm)
        ev.add("preflight", "CRC_guard", result="PASS", **info)
        if not re.fullmatch(r"26\.6\.\d+(?:[.-].*)?", build):
            raise Blocked("effective_RHBK_26_6_build_not_recorded")
        api = API(os.environ["RHBK_URL"], os.environ["RHBK_CA_FILE"], realm,
                  os.environ.get("ADMIN_CLIENT_ID", "rotation-admin"), os.environ["ADMIN_CLIENT_SECRET_FILE"])
        api.access()
        experiment = Experiment(api, ev, int(os.environ.get("TTL_PRINCIPAL", "300")),
                                int(os.environ.get("TTL_ROTATED", "120")),
                                int(os.environ.get("REMAINING_ROTATION", "60")),
                                int(os.environ.get("EPSILON", "5")))
        experiment.verify_profile()
        if api.http.clock_samples:
            offset = api.http.clock_samples[-1]["server_offset_seconds"]
            ev.add("preflight", "RHBK_HTTP_Date_clock_comparison", offset_seconds=offset)
            if abs(offset) > experiment.eps:
                raise Blocked("host_RHBK_clock_difference_exceeds_epsilon")
        else:
            sample = pod_clock_sample(build, experiment.eps)
            ev.add("preflight", "RHBK_pod_clock_comparison_HTTP_Date_absent", result="PASS", **sample)
            experiment.clock_checker = lambda: pod_clock_sample(build, experiment.eps)
        for scenario in scenarios:
            try:
                if scenario in ("early", "near", "expired"):
                    experiment.expiration(scenario)
                else:
                    getattr(experiment, scenario.replace("-", "_"))()
            except Blocked as exc:
                ev.results.append({"scenario": scenario, "result": "BLOCKED", "reason": str(exc)})
                ev.add(scenario, "blocked", result="BLOCKED", reason=str(exc))
                # A lost mutation is not retried; other independent scenarios remain safe.
        ev.add("preflight", "clock_samples_final", samples=api.http.clock_samples)
    except (Blocked, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        for scenario in scenarios:
            ev.results.append({"scenario": scenario, "result": "BLOCKED", "reason": reason})
        ev.add("preflight", "blocked", result="BLOCKED", reason=reason)
    summary = ev.finish()
    return 0 if summary["results"] and all(v["result"] == "PASS" for v in summary["results"]) else 2


if __name__ == "__main__":
    sys.exit(main())
