#!/usr/bin/env python3
"""Controlled operational experiments against the owned local CRC laboratory.

Never prints credentials, raw API resources, or raw pod logs. All mutations use
the dedicated kubeconfig and require recorded cluster/namespace UIDs and ownership.
This harness intentionally disrupts the LAB m2m-probe in T07/T16 only.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
NS = "rhbk-rotation-lab"
LABELS = {"app.kubernetes.io/part-of": "rhbk-secret-rotation-lab", "lab.rhbk/owner": "client-secret-rotation"}
SCENARIOS = {"T07": "delayed-rollout", "T10": "concurrency", "T11": "crash-recovery", "T12": "distribution-failure", "T16": "rollback"}
STATE_FIELDS = {"phase", "recovery_mode", "preexisting_outage", "expired_verified_at", "hold_skipped_reason", "generation", "next_generation", "old_generation", "current_secret", "next_secret", "old_secret", "cycle_id", "suspended", "post_started_at", "post_finished_at", "post_observed_at", "hold_until", "hold_origin", "rollout_started_at", "rollout_completed_at", "last_rotated_at", "old_retired_early", "old_retirement_verified_at", "old_probe_rejected", "old_probe_result", "baseline_auth", "blocked_reason", "pre_metadata", "post_metadata"}
LOG_FIELDS = {"timestamp", "event", "phase", "generation", "next_generation", "role", "http", "ok", "error", "latency_ms", "reason", "suspended", "started_at", "finished_at"}
STATUS_FIELDS = {"pod", "generation", "role", "attempts", "successes", "failures", "last_http", "last_success_at", "last_result", "latest_authentication_ok", "initial_authentication"}


class Blocked(Exception):
    pass


class Failed(Exception):
    pass


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require(value, reason):
    if not value:
        raise Failed(reason)


def event_epoch(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def fresh_rejection(pod, expiry, now=None):
    """Timed rejection sample; callers also require prior S1 success and valid S2."""
    now = time.time() if now is None else now
    attempted = event_epoch(pod.get("last_attempt_utc"))
    return (pod.get("last_http") in {400, 401} and pod.get("oauth_error") in {"invalid_client", "unauthorized_client"}
            and attempted is not None and attempted > expiry and -5 <= now-attempted <= 12)


class CRC:
    def __init__(self):
        self.run_id = "op-" + uuid.uuid4().hex[:10]
        self.events = []
        self.created_jobs = []
        self.observed_jobs = []
        self.started = time.time()
        self.started_mono = time.monotonic()
        self.guard()

    def oc(self, *args, body=None, allow_failure=False):
        kubeconfig = ROOT/".private/crc.kubeconfig"
        if not kubeconfig.is_file():
            raise Blocked("dedicated_crc_kubeconfig_missing_run_setup")
        try:
            result = subprocess.run(["oc", "--kubeconfig="+str(kubeconfig), "--request-timeout="+("0" if "--raw" in args and any("/proxy/" in str(a) for a in args) else "20s"), *args],
                input=None if body is None else json.dumps(body).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked("oc_unavailable_or_timed_out") from None
        if result.returncode and not allow_failure:
            # Raw error output can include payload details; never copy it to evidence.
            raise Blocked("oc_request_failed_" + str(result.returncode))
        return result.returncode, result.stdout.decode(errors="replace")

    def get(self, kind, name=None, cluster=False, optional=False):
        args = ["get", kind] + ([name] if name else []) + ([] if cluster else ["-n", NS]) + ["-o", "json"]
        if optional:
            args += ["--ignore-not-found"]
        _, value = self.oc(*args)
        return json.loads(value) if value.strip() else None

    def owned(self, value):
        if not value or any(value.get("metadata", {}).get("labels", {}).get(key) != val for key, val in LABELS.items()):
            raise Blocked("resource_ownership_mismatch")
        return value

    def guard(self):
        _, server = self.oc("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
        if server.strip() != "https://api.crc.testing:6443":
            raise Blocked("dedicated_kubeconfig_is_not_local_crc")
        expected = ROOT / "evidence/cluster-identity.json"
        if not expected.exists():
            raise Blocked("recorded_crc_identity_missing_run_preflight")
        identity = json.loads(expected.read_text())
        current = self.get("namespace", "kube-system", cluster=True)
        if current["metadata"]["uid"] != identity.get("uid"):
            raise Blocked("crc_cluster_uid_changed")
        namespace = self.owned(self.get("namespace", NS, cluster=True))
        try:
            namespace_identity = json.loads((ROOT/"evidence/namespace-identity.json").read_text())
        except (OSError, ValueError):
            raise Blocked("recorded_namespace_identity_missing_run_setup") from None
        if (not isinstance(namespace_identity, dict) or namespace_identity.get("name") != NS
                or namespace_identity.get("cluster_uid") != identity.get("uid")
                or namespace_identity.get("uid") != namespace.get("metadata", {}).get("uid")
                or not namespace_identity.get("uid")):
            raise Blocked("namespace_was_not_created_by_this_checkout")
        if abs((time.time() - self.started) - (time.monotonic() - self.started_mono)) > 3:
            raise Blocked("host_clock_jump_detected")

    def replace(self, obj):
        self.guard()
        self.owned(obj)
        if obj.get("metadata", {}).get("namespace") != NS or obj.get("kind") not in {"Deployment", "CronJob"}:
            raise Blocked("mutation_outside_harness_scope")
        if obj["metadata"]["name"] not in {"m2m-probe", "rotation-controller"}:
            raise Blocked("mutation_target_not_allowed")
        self.oc("replace", "-f", "-", body=obj)

    def record(self, event, **fields):
        row = {"timestamp_utc": utc(), "event": event, **fields}
        self.events.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    def state(self):
        resource = self.owned(self.get("configmap", "rotation-state"))
        value = json.loads(resource.get("data", {}).get("state.json", "{}"))
        # Controller schema contains no credential material; persist only known fields.
        return {key: value[key] for key in STATE_FIELDS if key in value}

    def cron_suspend(self):
        cron = self.owned(self.get("cronjob", "rotation-controller"))
        cron["spec"]["suspend"] = True
        self.replace(cron)
        self.record("cronjob_suspended")

    def no_live_jobs(self):
        active = [item for item in self.get("jobs")["items"]
                  if all(item["metadata"].get("labels", {}).get(k) == v for k, v in LABELS.items())
                  and not any(c.get("type") in {"Complete", "Failed"} and c.get("status") == "True" for c in item.get("status", {}).get("conditions", []))]
        if active:
            raise Blocked("another_lab_job_is_active")

    def launch(self, command, failure=None, suffix=None):
        self.guard()
        cron = self.owned(self.get("cronjob", "rotation-controller"))
        name = self.run_id + "-" + (suffix or command) + "-" + uuid.uuid4().hex[:4]
        spec = copy.deepcopy(cron["spec"]["jobTemplate"]["spec"])
        spec["backoffLimit"] = 0
        spec["activeDeadlineSeconds"] = 300
        template = spec["template"]
        template.setdefault("metadata", {}).setdefault("labels", {}).update({**LABELS, "lab.rhbk/operation-run": self.run_id})
        for container in template["spec"]["containers"]:
            if container["name"] == "controller":
                container["args"] = [command]
                container["env"] = [e for e in container.get("env", []) if e["name"] != "INJECT_FAILURE"]
                if failure:
                    container["env"].append({"name": "INJECT_FAILURE", "value": failure})
        obj = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NS,
               "labels": {**LABELS, "app.kubernetes.io/name": "rotation-controller", "lab.rhbk/operation-run": self.run_id}}, "spec": spec}
        self.oc("create", "-f", "-", body=obj)
        self.created_jobs.append(name)
        self.record("job_created", job=name, command=command, injected_failure=failure)
        return name

    def job_status(self, name):
        job = self.owned(self.get("job", name))
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("status") == "True" and condition.get("type") in {"Complete", "Failed"}:
                return condition["type"]
        return "Running"

    def logs(self, name):
        code, raw = self.oc("logs", "job/" + name, "-n", NS, "-c", "controller", "--tail=-1", allow_failure=True)
        if code:
            return []
        rows = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append({key: obj[key] for key in LOG_FIELDS if key in obj})
            except (ValueError, TypeError):
                continue
        return rows

    def wait_job(self, name, expected="Complete", timeout=310):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.job_status(name)
            if status != "Running":
                self.record("job_finished", job=name, status=status, controller_events=self.logs(name))
                require(status == expected, "job_did_not_finish_as_expected_" + expected)
                return status
            time.sleep(2)
        raise Blocked("job_completion_timeout")

    def run_job(self, command, failure=None, expected="Complete"):
        name = self.launch(command, failure)
        self.wait_job(name, expected)
        return name

    def samples(self):
        _, raw = self.oc("get", "pods", "-n", NS, "-l", "app.kubernetes.io/name=m2m-probe", "-o", "json")
        values = []
        for pod in json.loads(raw)["items"]:
            self.owned(pod)
            if pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running":
                continue
            name = pod["metadata"]["name"]
            _, raw_status = self.oc("get", "--raw", "/api/v1/namespaces/"+NS+"/pods/"+name+":8080/proxy/status", allow_failure=True)
            try:
                data = json.loads(raw_status)
                if not isinstance(data, dict) or data.get("pod") != name:
                    continue
            except ValueError:
                continue
            value = {key: data[key] for key in STATUS_FIELDS if key in data}
            last = data.get("last", {})
            value["oauth_error"] = last.get("oauth_error") if last.get("oauth_error") in {"invalid_client", "unauthorized_client", "invalid_request", "server_error", "temporarily_unavailable"} else None
            value["last_attempt_utc"] = last.get("timestamp_utc")
            value["ready"] = any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []))
            values.append(value)
        return values

    def healthy(self, generation):
        pods = self.samples()
        desired = self.get("deployment", "m2m-probe")["spec"].get("replicas", 2)
        return len(pods) == desired and all(p.get("generation") == f"v{generation:04d}" and p.get("last_http") == 200 and p.get("ready") and p.get("last_success_at") and -5 <= time.time()-p["last_success_at"] < 12 for p in pods)

    def wait_healthy(self, generation, timeout=65):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if self.healthy(generation):
                self.record("generation_authenticated", generation=generation, pods=self.samples())
                return
            time.sleep(2)
        raise Failed("deployment_did_not_authenticate_expected_generation")

    def wait_until(self, epoch, sample=True):
        while time.time() < epoch:
            if abs((time.time()-self.started)-(time.monotonic()-self.started_mono)) > 3:
                raise Blocked("host_clock_jump_detected")
            if sample:
                self.record("main_observation", pods=self.samples())
            time.sleep(min(2, max(0, epoch-time.time())))

    def precondition(self):
        self.cron_suspend()
        self.no_live_jobs()
        state = self.state()
        if state.get("phase") not in {"IDLE", "COMPLETE"}:
            raise Blocked("pending_cycle_requires_reconciliation_before_experiment")
        self.wait_healthy(state["generation"], 10)
        self.record("baseline", state=state, pods=self.samples())
        return state

    def template(self):
        return copy.deepcopy(self.owned(self.get("deployment", "m2m-probe"))["spec"]["template"])

    def set_template(self, template):
        deployment = self.owned(self.get("deployment", "m2m-probe"))
        deployment["spec"]["template"] = copy.deepcopy(template)
        self.replace(deployment)
        self.record("explicit_rollout", generation=template["metadata"]["labels"].get("lab.rhbk/generation"))


def assert_single_rotation(crc, before, after, jobs):
    require(after.get("phase") == "COMPLETE", "cycle_not_complete")
    require(after.get("generation") == before["generation"] + 1, "unexpected_generation_increment")
    require(after.get("cycle_id") and after["cycle_id"] != before.get("cycle_id"), "cycle_identity_did_not_advance")
    rows = [row for name in jobs for row in crc.logs(name)]
    rotating = sum(event.get("event") == "state_transition" and event.get("phase") == "ROTATING" for event in rows)
    responses = [event for event in rows if event.get("event") == "rotation_response_received"]
    require(len(responses) == 1 and responses[0].get("http") == 200
            and responses[0].get("generation") == f"v{after['generation']:04d}", "one_successful_post_response_not_observed")
    require(rotating == 1, "expected_exactly_one_persisted_post_intent_across_jobs")
    require(after.get("old_probe_rejected") is True, "old_expiry_not_confirmed")
    require(any(row.get("event") == "authentication" and row.get("role") == "old_after_expiry"
                and row.get("generation") == f"v{before['generation']:04d}" and row.get("ok") is False
                and row.get("http") in {400, 401} and row.get("error") in {"invalid_client", "unauthorized_client"} for row in rows),
            "old_expiry_has_no_fresh_authentication_log")
    require(any(row.get("event") == "authentication" and row.get("role") == "baseline"
                and row.get("generation") == f"v{before['generation']:04d}" and row.get("ok") is True
                and row.get("http") == 200 for row in rows), "old_credential_has_no_successful_baseline_log")
    require(any(row.get("event") == "authentication" and row.get("role") == "main_after_old_expiry"
                and row.get("generation") == f"v{after['generation']:04d}" and row.get("ok") is True
                and row.get("http") == 200 for row in rows), "new_credential_has_no_positive_expiry_control")
    crc.record("one_rotation_reconciled", rotating_transitions=rotating, final_state=after,
               evidence_limit="one observed POST response, reconciled lineage and fresh old rejection; not a server audit-event count")


def failure_recovery(crc, failure):
    before = crc.precondition()
    failed = crc.run_job("rotate-once", failure, "Failed")
    interrupted = crc.state()
    expected_phase = "ROLLING_OUT" if failure == "rollout" else "ROTATING"
    require(interrupted.get("phase") == expected_phase, "failure_did_not_reach_expected_cut_point")
    require(interrupted.get("next_generation") == before["generation"]+1, "intention_not_persisted")
    crc.record("fault_cut_point", failure=failure, state=interrupted, pods=crc.samples())
    recovered = crc.run_job("reconcile")
    after = crc.state()
    assert_single_rotation(crc, before, after, [failed, recovered])
    crc.wait_healthy(after["generation"])
    return after


def delayed_rollout(crc):
    before = crc.precondition()
    failed = crc.run_job("rotate-once", "rollout", "Failed")
    state = crc.state()
    require(state.get("phase") == "ROLLING_OUT", "rollout_failure_not_persisted")
    crc.wait_healthy(before["generation"], 5)
    metadata = state.get("post_metadata", {})
    expiry = metadata.get("client.secret.rotated.expiration.time")
    principal_expiry = metadata.get("client.secret.expiration.time")
    if not expiry or not principal_expiry:
        raise Blocked("expiration_metadata_unavailable")
    if principal_expiry < expiry+80:
        raise Blocked("insufficient_new_secret_validity_to_measure_delay_and_recover")
    crc.wait_until(expiry+6)
    observed = crc.samples()
    replicas = crc.get("deployment", "m2m-probe")["spec"].get("replicas", 2)
    require(len(observed) == replicas and all(p.get("generation") == f"v{before['generation']:04d}"
            and fresh_rejection(p, expiry) for p in observed), "fresh_old_main_rejection_not_observed_for_all_replicas_after_expiry")
    crc.record("expected_main_authentication_outage", pods=observed, old_expiration=expiry)
    recovered = crc.run_job("reconcile")
    after = crc.state()
    assert_single_rotation(crc, before, after, [failed,recovered])
    crc.wait_healthy(after["generation"])


def concurrency(crc):
    before = crc.precondition()
    cron = crc.get("cronjob", "rotation-controller")
    cron_uid = cron["metadata"]["uid"]
    try:
        # Wait with CronJob suspended, then enable it shortly before a real boundary.
        boundary = (int(time.time())//60+1)*60
        if boundary-time.time()<35:
            boundary += 60
        crc.wait_until(boundary-25, sample=False)
        crc.run_job("resume")
        if time.time()>boundary-5:
            raise Blocked("resume_job_missed_concurrency_window")
        launch_at=time.time()
        manual = crc.launch("rotate-once", suffix="manual")
        scheduled = []
        deadline = time.monotonic()+75
        while time.monotonic() < deadline and not scheduled:
            for job in crc.get("jobs")["items"]:
                refs = job["metadata"].get("ownerReferences", [])
                created = dt.datetime.fromisoformat(job["metadata"]["creationTimestamp"].replace("Z","+00:00")).timestamp()
                if created >= launch_at-2 and any(ref.get("uid") == cron_uid and ref.get("kind") == "CronJob" for ref in refs):
                    scheduled.append(job["metadata"]["name"])
            if not scheduled:
                time.sleep(1)
        crc.cron_suspend()
        if not scheduled:
            raise Blocked("real_cronjob_did_not_schedule_within_test_window")
        require(len(scheduled) == 1, "multiple_scheduled_jobs_entered_measurement_window")
        crc.observed_jobs.extend(scheduled)
        crc.record("real_scheduled_job", job=scheduled[0], manual_job=manual)
        limit = time.monotonic()+310
        jobs=[manual,*scheduled]
        while time.monotonic()<limit and any(crc.job_status(j)=="Running" for j in jobs):
            time.sleep(2)
        states=[crc.job_status(j) for j in jobs]
        require(states.count("Complete")==1 and states.count("Failed")==1, "expected_one_completion_and_one_lease_rejection")
        rows=[event for job in jobs for event in crc.logs(job)]
        require(any(event.get("event")=="controller_error" and "lease" in event.get("reason","") for event in rows), "concurrent_execution_not_refused_by_client_lease")
        for job in jobs:
            crc.record("concurrent_job_result",job=job,status=crc.job_status(job),controller_events=crc.logs(job))
        assert_single_rotation(crc,before,crc.state(),jobs)
        crc.wait_healthy(before["generation"]+1)
    finally:
        crc.cron_suspend()


def rollback(crc):
    before=crc.precondition()
    old_template=crc.template()
    new_template=None
    running=crc.launch("rotate-once")
    try:
        deadline=time.monotonic()+100
        state=crc.state()
        while state.get("phase")!="VERIFYING" and time.monotonic()<deadline:
            if crc.job_status(running)!="Running":
                raise Failed("rotation_ended_before_rollback_window")
            time.sleep(1)
            state=crc.state()
        require(state.get("phase")=="VERIFYING","rollout_did_not_enter_verification_window")
        new_template=crc.template()
        expiry=state["post_metadata"]["client.secret.rotated.expiration.time"]
        if time.time()+55>=expiry:
            raise Blocked("insufficient_overlap_for_two_controlled_rollouts")
        crc.set_template(old_template)
        crc.wait_healthy(before["generation"],25)
        crc.record("rollback_during_overlap_authenticated",old_expiration=expiry,pods=crc.samples())
        crc.set_template(new_template)
        crc.wait_healthy(before["generation"]+1,25)
        crc.wait_job(running)
        assert_single_rotation(crc,before,crc.state(),[running])
        crc.set_template(old_template)
        limit=time.monotonic()+30
        denied=[]
        while time.monotonic()<limit:
            pods=crc.samples()
            denied=[p for p in pods if p.get("generation")==f"v{before['generation']:04d}" and fresh_rejection(p, expiry)]
            if denied:break
            time.sleep(2)
        require(denied,"expired_rollback_credential_not_observed_rejected")
        require(all(not p.get("ready") for p in denied),"expired_rollback_pod_became_ready")
        crc.record("rollback_after_expiration_rejected",old_pods=denied,all_main_pods=crc.samples())
    finally:
        if new_template:
            crc.set_template(new_template)
            crc.wait_healthy(before["generation"]+1)
            crc.record("forward_recovery_used_existing_generation",generation=before["generation"]+1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario",choices=[*SCENARIOS,*SCENARIOS.values()])
    parser.add_argument("--fault",choices=["secret-write","rollout","both"],default="both",help="T12 distribution cut point")
    args=parser.parse_args()
    scenario=args.scenario if args.scenario in SCENARIOS else next(k for k,v in SCENARIOS.items() if v==args.scenario)
    crc=None
    report={"scenario":scenario,"name":SCENARIOS[scenario],"started_utc":utc(),"status":"BLOCKED","scope":"real CRC/RHBK only"}
    try:
        crc=CRC()
        if scenario=="T07":delayed_rollout(crc)
        elif scenario=="T10":concurrency(crc)
        elif scenario=="T11":failure_recovery(crc,"after-post")
        elif scenario=="T12":
            for fault in (["secret-write","rollout"] if args.fault=="both" else [args.fault]):
                failure_recovery(crc,fault)
        elif scenario=="T16":rollback(crc)
        report.update(status="PASS",reason="all_scenario_assertions_satisfied")
    except Blocked as exc:
        report.update(status="BLOCKED",reason=str(exc))
    except Failed as exc:
        report.update(status="FAIL",reason=str(exc))
    except (KeyError,ValueError,TypeError):
        report.update(status="BLOCKED",reason="unexpected_runtime_schema_or_configuration")
    finally:
        if crc:
            try:
                crc.cron_suspend()
                active=[j for j in crc.created_jobs+crc.observed_jobs if crc.job_status(j)=="Running"]
                if not active:
                    crc.run_job("suspend")
                else:
                    report["active_jobs_requiring_followup"]=active
                    if report["status"] == "PASS":
                        report.update(status="BLOCKED", reason="scenario_passed_but_active_jobs_remain")
                report["final_state"]=crc.state()
            except (Blocked,Failed,KeyError,ValueError,TypeError):
                report["final_suspend_warning"]="cronjob_or_state_suspension_requires_followup"
                if report["status"] == "PASS":
                    report.update(status="BLOCKED", reason="scenario_passed_but_final_suspension_not_verified")
            report["events"]=crc.events
            report["created_jobs"]=crc.created_jobs
        report["finished_utc"]=utc()
        path=ROOT/"evidence/operations"/(scenario+"-"+dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+".json")
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k not in {"events","final_state"}},sort_keys=True))
    return 0 if report["status"]=="PASS" else (2 if report["status"]=="BLOCKED" else 1)


if __name__=="__main__":
    raise SystemExit(main())
