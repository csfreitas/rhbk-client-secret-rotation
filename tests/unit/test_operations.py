"""Independent assertions for the real-cluster experiment harness (no cluster I/O)."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "integration/operations.py"
spec = importlib.util.spec_from_file_location("lab_operations_tests", SOURCE)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class Evidence:
    def __init__(self, rows): self.rows, self.events = rows, []
    def logs(self, name): return self.rows
    def record(self, event, **values): self.events.append({"event":event,**values})


class OperationsTests(unittest.TestCase):
    def test_missing_dedicated_kubeconfig_never_uses_ambient_context(self):
        crc=object.__new__(ops.CRC)
        with tempfile.TemporaryDirectory() as directory, patch.object(ops,'ROOT',Path(directory)), patch.object(ops.subprocess,'run') as run:
            with self.assertRaisesRegex(ops.Blocked,'dedicated_crc_kubeconfig_missing'):
                crc.oc('get','pods')
            run.assert_not_called()

    def test_guard_refuses_namespace_identity_from_another_checkout(self):
        crc=object.__new__(ops.CRC)
        crc.started=ops.time.time();crc.started_mono=ops.time.monotonic()
        crc.oc=lambda *args:(0,'https://api.crc.testing:6443')
        crc.get=lambda kind,name,**kwargs:({'metadata':{'uid':'cluster-test'}} if name=='kube-system' else {'metadata':{'uid':'namespace-test','labels':ops.LABELS}})
        with tempfile.TemporaryDirectory() as directory, patch.object(ops,'ROOT',Path(directory)):
            evidence=Path(directory)/'evidence';evidence.mkdir()
            (evidence/'cluster-identity.json').write_text(json.dumps({'uid':'cluster-test'}))
            (evidence/'namespace-identity.json').write_text(json.dumps({'name':ops.NS,'uid':'other','cluster_uid':'cluster-test'}))
            with self.assertRaisesRegex(ops.Blocked,'not_created_by_this_checkout'):crc.guard()
            (evidence/'namespace-identity.json').write_text(json.dumps({'name':ops.NS,'uid':'namespace-test','cluster_uid':'cluster-test'}))
            crc.guard()

    def test_fresh_invalid_client_after_expiry_is_required(self):
        sample={"last_http":401,"oauth_error":"invalid_client","last_attempt_utc":ops.utc(106)}
        self.assertTrue(ops.fresh_rejection(sample,100,110))
        self.assertTrue(ops.fresh_rejection({**sample,"oauth_error":"unauthorized_client"},100,110))
        for mutation in [{"last_attempt_utc":ops.utc(99)}, {"last_attempt_utc":ops.utc(80)},
                         {"last_attempt_utc":None}, {"last_attempt_utc":"invalid"},
                         {"last_attempt_utc":ops.utc(130)}, {"last_http":0},
                         {"oauth_error":"network_error"}, {"last_http":503}]:
            self.assertFalse(ops.fresh_rejection({**sample,**mutation},100,110))
    def test_timezone_is_required_for_event_timestamp(self):
        self.assertIsNone(ops.event_epoch("2026-11-09T17:00:00"))
        self.assertIsNotNone(ops.event_epoch("2026-11-09T17:00:00Z"))
    def test_complete_flag_without_old_rejection_log_cannot_pass(self):
        before={"generation":1}
        after={"phase":"COMPLETE","generation":2,"cycle_id":"fresh","old_probe_rejected":True}
        rows=[{"event":"state_transition","phase":"ROTATING"},
              {"event":"rotation_response_received","http":200,"generation":"v0002"}]
        with self.assertRaises(ops.Failed): ops.assert_single_rotation(Evidence(rows),before,after,["job"])
        rows.append({"event":"authentication","role":"old_after_expiry","generation":"v0001","ok":False,"http":401,"error":"invalid_client"})
        rows.extend([
            {"event":"authentication","role":"baseline","generation":"v0001","ok":True,"http":200},
            {"event":"authentication","role":"main_after_old_expiry","generation":"v0002","ok":True,"http":200}])
        evidence=Evidence(rows)
        ops.assert_single_rotation(evidence,before,after,["job"])
        self.assertEqual(len(evidence.events),1)
    def test_unauthorized_rejection_requires_prior_success_and_new_control(self):
        before={"generation":1}
        after={"phase":"COMPLETE","generation":2,"cycle_id":"fresh","old_probe_rejected":True}
        rows=[{"event":"state_transition","phase":"ROTATING"},
              {"event":"rotation_response_received","http":200,"generation":"v0002"},
              {"event":"authentication","role":"old_after_expiry","generation":"v0001","ok":False,"http":401,"error":"unauthorized_client"}]
        with self.assertRaises(ops.Failed): ops.assert_single_rotation(Evidence(rows),before,after,["job"])
        rows.append({"event":"authentication","role":"baseline","generation":"v0001","ok":True,"http":200})
        with self.assertRaises(ops.Failed): ops.assert_single_rotation(Evidence(rows),before,after,["job"])
        rows.append({"event":"authentication","role":"main_after_old_expiry","generation":"v0002","ok":True,"http":200})
        ops.assert_single_rotation(Evidence(rows),before,after,["job"])
    def test_no_post_response_is_not_equivalent_to_successful_post(self):
        rows=[{"event":"state_transition","phase":"ROTATING"},
              {"event":"authentication","role":"old_after_expiry","generation":"v0001","ok":False,"http":401,"error":"invalid_client"}]
        after={"phase":"COMPLETE","generation":2,"cycle_id":"fresh","old_probe_rejected":True}
        with self.assertRaises(ops.Failed): ops.assert_single_rotation(Evidence(rows),{"generation":1},after,["job"])
    def test_same_cycle_identity_cannot_be_reused_as_new_evidence(self):
        with self.assertRaises(ops.Failed):
            ops.assert_single_rotation(Evidence([]),{"generation":1,"cycle_id":"same"},
                {"phase":"COMPLETE","generation":2,"cycle_id":"same"},["job"])
    def test_duplicate_post_responses_fail_single_rotation_assertion(self):
        rows=[{"event":"state_transition","phase":"ROTATING"}]
        rows.extend([{"event":"rotation_response_received","http":200,"generation":"v0002"}]*2)
        with self.assertRaises(ops.Failed):
            ops.assert_single_rotation(Evidence(rows),{"generation":1},
                {"phase":"COMPLETE","generation":2,"cycle_id":"fresh","old_probe_rejected":True},["job"])
    def test_future_clock_skew_does_not_count_as_healthy(self):
        crc=object.__new__(ops.CRC)
        crc.get=lambda *args: {"spec":{"replicas":2}}
        crc.samples=lambda: [{"generation":"v0001","last_http":200,"ready":True,"last_success_at":130}]*2
        with patch.object(ops.time,"time",return_value=100):
            self.assertFalse(crc.healthy(1))
        crc.samples=lambda: [{"generation":"v0001","last_http":200,"ready":True,"last_success_at":99}]*2
        with patch.object(ops.time,"time",return_value=100):
            self.assertTrue(crc.healthy(1))
    def test_final_suspension_failure_prevents_pass_report(self):
        class CRC:
            events=[]
            created_jobs=[]
            observed_jobs=[]
            def cron_suspend(self): raise ops.Blocked("unavailable")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ops,"ROOT",Path(directory)), patch.object(ops,"CRC",CRC), \
             patch.object(ops,"delayed_rollout",lambda crc: None), \
             patch.object(ops.sys,"argv",["operations.py","T07"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ops.main(),2)
            report=json.loads(next((Path(directory)/"evidence/operations").glob("*.json")).read_text())
            self.assertEqual(report["status"],"BLOCKED")
            self.assertEqual(report["reason"],"scenario_passed_but_final_suspension_not_verified")


if __name__ == "__main__": unittest.main()
