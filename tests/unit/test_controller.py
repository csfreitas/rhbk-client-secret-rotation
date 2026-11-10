"""Fault-injection tests. These do not validate RHBK/CRC runtime behavior."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[2] / "apps/rotation-controller/controller.py"
spec = importlib.util.spec_from_file_location("controller", MODULE)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class Clock:
    def __init__(self, now=180): self.now = now
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


class State:
    def __init__(self):
        self.value = {"phase": "IDLE", "generation": 1, "current_secret": os.getenv("SECRET_PREFIX", "catalogo-api-cred") + "-v0001", "last_rotated_at": 0, "suspended": False}
        self.saves = []
    def save(self, **changes):
        self.value.update(changes)
        self.saves.append(copy.deepcopy(self.value))


class HeldLease:
    def __init__(self): self.lost = False
    def check(self):
        if self.lost: raise c.SafeError("lease lost")


class KC:
    def __init__(self, clock):
        self.clock, self.posts, self.deletes = clock, 0, 0
        self.current, self.rotated = "FAKE_OLD_CREDENTIAL", None
        self.metadata_value = {c.META_KEYS[0]: 0, c.META_KEYS[1]: 300}
    def resolve(self, client): return "uuid-test"
    def metadata(self, uid): return copy.deepcopy(self.metadata_value)
    def secret(self, uid, rotated=False): return self.rotated if rotated else self.current
    def rotate(self, uid):
        self.posts += 1
        self.rotated, self.current = self.current, "FAKE_NEW_CREDENTIAL_" + str(self.posts)
        now = self.clock.time()
        self.metadata_value = {c.META_KEYS[0]: now, c.META_KEYS[1]: now+300, c.META_KEYS[2]: 0, c.META_KEYS[3]: now+120}
        return self.current, {"started": now, "finished": now}
    def invalidate_rotated(self, uid):
        self.deletes += 1
        self.rotated = None
    def authenticate(self, client, value):
        valid = (value == self.current and self.clock.time() <= self.metadata_value[c.META_KEYS[1]]) or (value == self.rotated and self.clock.time() <= self.metadata_value.get(c.META_KEYS[3], 0))
        return {"http": 200 if valid else 401, "ok": valid, "error": None if valid else "invalid_client", "timestamp": c.utc(self.clock.time())}


class Controller(c.Controller):
    def __init__(self):
        self.fakeclock, self.fakestate, self.fakelease = Clock(), State(), HeldLease()
        self.fakekc = KC(self.fakeclock)
        # Existing timing regressions explicitly exercise the original 180s profile.
        with patch.dict(os.environ, {"ROTATE_INTERVAL": os.getenv("ROTATE_INTERVAL", "180")}):
            super().__init__(None, self.fakekc, self.fakestate, self.fakelease, self.fakeclock.time, self.fakeclock.sleep)
        self.credentials = {self.state.value["current_secret"]: self.fakekc.current}
        self.pod_generation, self.rollouts, self.old_probes = 1, 0, []
    def credential(self, name): return self.credentials[name]
    def verify_pods(self, generation): return self.pod_generation == generation
    def old_probe(self, generation): self.old_probes.append(generation)
    def stage(self, value):
        if os.getenv("INJECT_FAILURE") == "secret-write": raise c.SafeError("injected secret failure")
        self.credentials[self.state.value["next_secret"]] = value
    def start_rollout(self):
        if os.getenv("INJECT_FAILURE") == "rollout": raise c.SafeError("injected rollout failure")
        self.rollouts += 1
        self.pod_generation = self.state.value["next_generation"]


class FakeKube:
    coordination = "/leases-api"
    core = "/core"
    def __init__(self): self.objects, self.version = {}, 0; self.mutex = threading.Lock()
    owned = staticmethod(c.Kube.owned)
    def get(self, path, optional=False):
        with self.mutex:
            if path in self.objects: return copy.deepcopy(self.objects[path])
            if optional: return None
            raise c.APIError(404)
    def create(self, path, obj):
        with self.mutex:
            path += "/" + obj["metadata"]["name"]
            if path in self.objects: raise c.APIError(409)
            self.version += 1
            result = copy.deepcopy(obj)
            result["metadata"]["resourceVersion"] = str(self.version)
            self.objects[path] = result
            return copy.deepcopy(result)
    def replace(self, path, obj):
        with self.mutex:
            if self.objects[path]["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]: raise c.APIError(409)
            self.version += 1
            result = copy.deepcopy(obj)
            result["metadata"]["resourceVersion"] = str(self.version)
            self.objects[path] = result
            return copy.deepcopy(result)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()
        self.stdout = io.StringIO()
        self.output = contextlib.redirect_stdout(self.stdout)
        self.output.__enter__()
    def tearDown(self): self.output.__exit__(None,None,None); self.environ.stop()
    def expired_controller(self):
        ctl=Controller();ctl.fakeclock.now=310;ctl.state.value["suspended"]=True
        kube=FakeKube();kube.batch="/batch"
        kube.create("/batch/cronjobs",{"metadata":{"name":"rotation-controller","labels":c.LABELS},"spec":{"suspend":True}})
        ctl.kube=kube
        ctl.deployment_object=lambda:{"metadata":{"labels":c.LABELS},"spec":{"template":{"metadata":{"labels":{"lab.rhbk/generation":"v0001"}},"spec":{"containers":[{"name":"probe","env":[{"name":k,"valueFrom":{"secretKeyRef":{"name":"catalogo-api-cred-v0001","key":v}}} for k,v in zip(("CLIENT_SECRET","CLIENT_ID","GENERATION"),("client-secret","client-id","generation"))]}]}}}}
        return ctl
    def test_expired_recovery_records_outage_and_invalidates_previous_before_rollout(self):
        ctl=self.expired_controller();ctl.recover_expired()
        self.assertEqual(ctl.state.value["phase"],"COMPLETE")
        self.assertEqual(ctl.fakekc.posts,1);self.assertEqual(ctl.fakekc.deletes,1)
        self.assertTrue(ctl.state.value["preexisting_outage"])
        self.assertFalse(ctl.state.value["baseline_auth"]["ok"])
        self.assertTrue(ctl.state.value["old_retired_early"])
        self.assertEqual(ctl.state.value["history"][-1]["old_rejection_basis"],"explicit-invalidation")
        self.assertEqual(ctl.state.value["rollout_started_at"],310)
        self.assertEqual(ctl.old_probes,[])
        self.assertNotIn("HOLDING_OLD",[row["phase"] for row in ctl.state.saves])
    def test_expired_recovery_crash_after_post_reuses_same_secret(self):
        ctl=self.expired_controller()
        with patch.dict(os.environ,{"INJECT_FAILURE":"after-post"}):
            with self.assertRaises(c.SafeError):ctl.recover_expired()
        with self.assertRaises(c.SafeError):ctl.recover_expired()
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts,1);self.assertEqual(ctl.fakekc.deletes,1)
        self.assertEqual(ctl.state.value["phase"],"COMPLETE")
    def test_expired_recovery_crash_after_delete_reads_back_absence_without_another_post(self):
        ctl=self.expired_controller()
        with patch.dict(os.environ,{"INJECT_FAILURE":"after-retire-delete"}):
            with self.assertRaises(c.SafeError):ctl.recover_expired()
        self.assertEqual(ctl.state.value["phase"],"RETIRING_OLD")
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts,1);self.assertEqual(ctl.fakekc.deletes,1)
        self.assertEqual(ctl.state.value["phase"],"COMPLETE")
    def test_expired_recovery_without_retained_lineage_blocks_without_second_post(self):
        ctl=self.expired_controller();original=ctl.fakekc.rotate
        def rotate(uid):
            result=original(uid);ctl.fakekc.rotated=None;return result
        ctl.fakekc.rotate=rotate
        with self.assertRaises(c.SafeError):ctl.recover_expired()
        self.assertEqual(ctl.state.value["phase"],"BLOCKED")
        self.assertEqual(ctl.fakekc.posts,1);self.assertEqual(ctl.fakekc.deletes,0)
    def test_expired_recovery_requires_suspended_automation(self):
        ctl=self.expired_controller();ctl.state.value["suspended"]=False
        with self.assertRaises(c.SafeError):ctl.recover_expired()
        self.assertEqual(ctl.fakekc.posts,0)
    def test_expired_recovery_refuses_valid_or_unproven_expiry(self):
        ctl=self.expired_controller();ctl.fakeclock.now=250
        with self.assertRaises(c.SafeError):ctl.recover_expired()
        self.assertEqual(ctl.fakekc.posts,0)
    def test_expired_recovery_refuses_network_failure_as_expiration(self):
        ctl=self.expired_controller();ctl.fakekc.authenticate=lambda *args:{"ok":False,"http":0,"error":"network_error"}
        with self.assertRaises(c.SafeError):ctl.recover_expired()
        self.assertEqual(ctl.fakekc.posts,0)
    def test_recovery_delete_refuses_changed_principal(self):
        ctl=self.expired_controller()
        with patch.dict(os.environ,{"INJECT_FAILURE":"after-retire-delete"}):
            with self.assertRaises(c.SafeError):ctl.recover_expired()
        ctl.fakekc.current="FAKE_FOREIGN_PRINCIPAL"
        with self.assertRaises(c.SafeError):ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts,1);self.assertEqual(ctl.fakekc.deletes,1)
        self.assertEqual(ctl.state.value["phase"],"BLOCKED")
    def test_normal_cycle_after_recovery_clears_outage_mode_and_uses_hold(self):
        ctl=self.expired_controller();ctl.recover_expired()
        ctl.fakeclock.now=490;ctl.run(manual=True)
        self.assertIsNone(ctl.state.value["recovery_mode"])
        self.assertFalse(ctl.state.value["preexisting_outage"])
        self.assertFalse(ctl.state.value["old_retired_early"])
        self.assertEqual(ctl.state.value["history"][-1]["old_rejection_basis"],"natural-expiration")
        self.assertEqual(ctl.fakekc.deletes,1)
    def test_full_cycle_waits_rollout_and_rejects_old(self):
        ctl = Controller(); ctl.run()
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.rollouts, 1)
        self.assertEqual(ctl.state.value["generation"], 2)
        self.assertGreaterEqual(ctl.state.value["rollout_started_at"], 225)
        self.assertTrue(ctl.state.value["old_probe_rejected"])
        self.assertGreaterEqual(ctl.fakeclock.time(), 305)
    def test_three_periodic_cycles_use_three_posts_and_monotonic_generations(self):
        ctl = Controller()
        for number, epoch in enumerate([180, 360, 540], start=2):
            ctl.fakeclock.now = epoch
            ctl.run()
            self.assertEqual(ctl.state.value["phase"], "COMPLETE")
            self.assertEqual(ctl.state.value["generation"], number)
        self.assertEqual(ctl.fakekc.posts, 3)
        self.assertEqual(len(ctl.state.value["history"]), 3)
    def test_rollout_recovery_after_old_expiry_uses_still_valid_new_secret(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"rollout"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.fakeclock.now = 310
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
    def test_recovery_never_rolls_out_already_expired_new_generation(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"rollout"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.fakeclock.now = 490
        with self.assertRaises(c.SafeError): ctl.run(reconcile_only=True)
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.rollouts, 0)
    def test_late_crash_recovery_stages_secret_but_blocks_unsafe_hold(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"after-post"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.fakeclock.now = 250
        with self.assertRaises(c.SafeError): ctl.run(reconcile_only=True)
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertIn("catalogo-api-cred-v0002", ctl.credentials)
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.rollouts, 0)
    def test_scheduler_jitter_with_extended_old_lifetime(self):
        ctl = Controller(); ctl.fakeclock.now = 230
        ctl.run()
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
        self.assertEqual(ctl.fakekc.posts, 1)
    def test_insufficient_original_validity_stops_before_post(self):
        ctl = Controller(); ctl.fakeclock.now = 290
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.fakekc.posts, 0)
        self.assertEqual(ctl.state.value["phase"], "IDLE")
    def test_realistic_minute_jitter_completes_three_cycles_without_shortening_interval(self):
        ctl=Controller()
        for actual, premature in [(180.2,360.1),(420.3,600.2),(660.4,840.3)]:
            ctl.fakeclock.now=actual
            ctl.run()
            generation=ctl.state.value["generation"]
            posts=ctl.fakekc.posts
            ctl.fakeclock.now=premature
            ctl.run()
            self.assertEqual(ctl.state.value["generation"],generation)
            self.assertEqual(ctl.fakekc.posts,posts)
        self.assertEqual(ctl.fakekc.posts,3)
        self.assertEqual(ctl.state.value["generation"],4)
        self.assertEqual(ctl.interval,180)
    def test_original_validity_rechecked_after_intent_before_post_boundary(self):
        ctl=Controller(); original=ctl.fakekc.metadata; reads=[]
        def metadata(uid):
            reads.append(True)
            if len(reads)==2:ctl.fakeclock.now=290
            return original(uid)
        ctl.fakekc.metadata=metadata
        with self.assertRaises(c.SafeError):ctl.run()
        self.assertEqual(ctl.fakekc.posts,0)
        self.assertEqual(ctl.state.value["phase"],"INTENT_RECORDED")
        self.assertFalse(any(s["phase"]=="ROTATING" for s in ctl.state.saves))
    def test_intent_resume_does_not_rotate_over_an_external_change(self):
        ctl=Controller()
        ctl.state.save(phase="INTENT_RECORDED",old_secret="catalogo-api-cred-v0001",old_generation=1,next_generation=2,next_secret="catalogo-api-cred-v0002")
        ctl.fakekc.current="FAKE_EXTERNAL_CREDENTIAL"
        with self.assertRaises(c.SafeError):ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts,0)
        self.assertEqual(ctl.state.value["phase"],"BLOCKED")
    def test_capped_old_deadline_cannot_pass_overlap_budget(self):
        ctl = Controller(); ctl.fakeclock.now = 210
        original=ctl.fakekc.rotate
        def capped(uid):
            result=original(uid)
            ctl.fakekc.metadata_value[c.META_KEYS[3]]=300
            return result
        ctl.fakekc.rotate=capped
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertEqual(ctl.rollouts, 0)
    def test_stale_configmap_writer_is_rejected(self):
        kube=FakeKube()
        kube.create(kube.core+"/configmaps",{"metadata":{"name":"rotation-state","labels":c.LABELS},"data":{"state.json":json.dumps(State().value)}})
        one, two = c.State(kube,"rotation-state"), c.State(kube,"rotation-state")
        one.save(phase="INTENT_RECORDED")
        with self.assertRaises(c.APIError): two.save(phase="ROTATING")
        self.assertEqual(c.State(kube,"rotation-state").value["phase"], "INTENT_RECORDED")
    def test_periodic_decision_does_not_rotate_each_evaluation(self):
        ctl = Controller(); ctl.fakeclock.now = 120; ctl.run()
        self.assertEqual(ctl.fakekc.posts, 0)
    def test_crash_after_post_reconciles_exactly_once(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"after-post"}):
            with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "ROTATING")
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertIsNone(ctl.state.value["post_finished_at"])
        self.assertEqual(ctl.state.value["hold_origin"], "principal_creation_plus_epsilon")
    def test_crash_recovery_job_start_delay_preserves_original_hold_anchor(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"after-post"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.fakeclock.now = 200
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
        self.assertEqual(ctl.state.value["hold_until"], 230)
        self.assertIsNone(ctl.state.value["post_finished_at"])
    def test_secret_write_failure_reuses_authoritative_new_secret(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"secret-write"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
    def test_rollout_failure_resumes_same_generation(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"rollout"}):
            with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "ROLLING_OUT")
        ctl.run(reconcile_only=True)
        self.assertEqual(ctl.fakekc.posts, 1)
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
    def test_ambiguous_rotation_blocks_without_retry(self):
        ctl = Controller()
        ctl.state.save(phase="ROTATING", old_secret="catalogo-api-cred-v0001", old_generation=1, next_generation=2, next_secret="catalogo-api-cred-v0002", post_started_at=180)
        with self.assertRaises(c.SafeError): ctl.run(reconcile_only=True)
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertEqual(ctl.fakekc.posts, 0)
        self.assertTrue(ctl.state.value["suspended"])
    def test_unrelated_extra_rotation_blocks_lineage(self):
        ctl = Controller()
        with patch.dict(os.environ, {"INJECT_FAILURE":"after-post"}):
            with self.assertRaises(c.SafeError): ctl.run()
        ctl.fakekc.rotated = "FAKE_OTHER_CREDENTIAL"
        with self.assertRaises(c.SafeError): ctl.run(reconcile_only=True)
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertEqual(ctl.fakekc.posts, 1)
    def test_lost_lock_prevents_mutation(self):
        ctl = Controller(); ctl.fakelease.lost = True
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.fakekc.posts, 0)
    def test_expiry_network_error_is_not_success(self):
        ctl = Controller(); original = ctl.fakekc.authenticate
        def auth(client, value):
            if ctl.fakeclock.time() >= 305 and value == "FAKE_OLD_CREDENTIAL": return {"ok":False,"http":0,"error":"network_error"}
            return original(client,value)
        ctl.fakekc.authenticate = auth
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "VERIFYING")
        self.assertFalse(ctl.state.value["old_probe_rejected"])
    def test_observed_unauthorized_client_with_positive_controls_completes(self):
        ctl = Controller(); original = ctl.fakekc.authenticate
        def auth(client, value):
            result = original(client,value)
            if not result["ok"] and value == "FAKE_OLD_CREDENTIAL":
                result["error"] = "unauthorized_client"
            return result
        ctl.fakekc.authenticate = auth
        ctl.run()
        self.assertEqual(ctl.state.value["phase"], "COMPLETE")
        self.assertTrue(ctl.state.value["baseline_auth"]["ok"])
        self.assertEqual(ctl.state.value["old_probe_result"]["error"], "unauthorized_client")
        self.assertEqual(ctl.state.value["history"][-1]["old_rejection_oauth"], "unauthorized_client")
    def test_unauthorized_old_with_failed_new_control_is_not_expiry_success(self):
        ctl = Controller(); original = ctl.fakekc.authenticate
        def auth(client, value):
            result = original(client,value)
            if ctl.fakeclock.time() >= 305:
                return {"ok":False,"http":401,"error":"unauthorized_client"}
            return result
        ctl.fakekc.authenticate = auth
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "BLOCKED")
        self.assertFalse(ctl.state.value["old_probe_rejected"])
    def test_permission_error_is_not_expiration_evidence(self):
        ctl = Controller(); original = ctl.fakekc.authenticate
        def auth(client, value):
            if ctl.fakeclock.time() >= 305 and value == "FAKE_OLD_CREDENTIAL":
                return {"ok":False,"http":403,"error":"access_denied"}
            return original(client,value)
        ctl.fakekc.authenticate = auth
        with self.assertRaises(c.SafeError): ctl.run()
        self.assertEqual(ctl.state.value["phase"], "VERIFYING")
        self.assertFalse(ctl.state.value["old_probe_rejected"])
    def test_no_credentials_in_state_or_logs(self):
        ctl = Controller(); ctl.run()
        serialized = json.dumps(ctl.fakestate.saves) + self.stdout.getvalue()
        self.assertNotIn("FAKE_OLD_CREDENTIAL", serialized)
        self.assertNotIn("FAKE_NEW_CREDENTIAL", serialized)
    def test_tls_cannot_be_disabled(self):
        with self.assertRaises(c.SafeError): c.HTTP("http://keycloak")
    def test_milliseconds_metadata_normalization(self):
        self.assertEqual(c.timestamp("1794240000000"), 1794240000)
    def test_invalid_overlap_budget_rejected(self):
        with patch.dict(os.environ,{"HOLD_SECONDS":"90"}):
            with self.assertRaises(c.SafeError): Controller()
    def test_concurrent_lease_only_one_holder(self):
        kube=FakeKube(); one=c.Lease(kube,"client"); two=c.Lease(kube,"client")
        one.acquire()
        try:
            with self.assertRaises(c.SafeError): two.acquire()
        finally: one.close()
        two.acquire(); two.close()
    def test_parallel_lease_contention_has_one_winner(self):
        kube = FakeKube()
        gate, done = threading.Barrier(8), threading.Event()
        winners, failures = [], []
        def contend():
            lease=c.Lease(kube,"race")
            gate.wait(timeout=5)
            try:
                lease.acquire()
                winners.append(lease)
                done.wait(timeout=5)
            except c.SafeError:
                failures.append(True)
            finally:
                if lease in winners: lease.close()
        threads=[threading.Thread(target=contend) for _ in range(8)]
        for thread in threads: thread.start()
        # Wait until all contenders have an outcome before releasing the winner.
        for _ in range(200):
            if len(winners)+len(failures)==8: break
            threading.Event().wait(0.005)
        done.set()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(len(winners),1)
        self.assertEqual(len(failures),7)
    def test_foreign_state_rejected(self):
        kube=FakeKube()
        kube.create(kube.core+"/configmaps",{"metadata":{"name":"rotation-state","labels":{}},"data":{"state.json":json.dumps(State().value)}})
        with self.assertRaises(c.SafeError): c.State(kube,"rotation-state")
    def test_nonfinite_timestamp_rejected(self):
        for value in ["nan","inf","-inf","0","-1"]:
            with self.assertRaises(ValueError): c.timestamp(value)
    def test_kubernetes_lease_microtime_has_exactly_six_fractional_digits(self):
        self.assertEqual(c.utc_micro(1794253146.306), "2026-11-09T19:39:06.306000Z")
        kube=FakeKube();lease=c.Lease(kube,"microtime").acquire()
        try:
            for key in ["acquireTime","renewTime"]:
                self.assertRegex(kube.get(lease.path)["spec"][key],r"T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
            lease.renew()
            self.assertRegex(kube.get(lease.path)["spec"]["renewTime"],r"T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
        finally:lease.close()
    def test_lease_cas_conflict(self):
        kube=FakeKube(); one=c.Lease(kube,"client").acquire()
        try:
            stale=kube.get(one.path); fresh=kube.get(one.path); kube.replace(one.path,fresh)
            with self.assertRaises(c.APIError): kube.replace(one.path,stale)
        finally: one.close()
    def test_metadata_excludes_rotated_secret_value(self):
        kc=object.__new__(c.Keycloak)
        kc.admin=lambda *args: (200,{"attributes":{c.META_KEYS[1]:"1700000000000","client.secret.rotated":"FAKE_SENSITIVE"}}, {})
        meta=kc.metadata("uuid")
        self.assertEqual(meta,{c.META_KEYS[1]:1700000000})
    def test_stage_immutable_collision_blocks(self):
        ctl=Controller(); kube=FakeKube(); ctl.kube=kube
        ctl.state.save(next_generation=2,next_secret="catalogo-api-cred-v0002")
        kube.create(kube.core+"/secrets",{"metadata":{"name":"catalogo-api-cred-v0002","labels":c.LABELS},"immutable":True,"data":{"client-secret":base64.b64encode(b"FAKE_OTHER").decode()}})
        with self.assertRaises(c.SafeError): c.Controller.stage(ctl,"FAKE_NEW")
        self.assertEqual(ctl.state.value["phase"],"BLOCKED")


if __name__ == "__main__": unittest.main()
