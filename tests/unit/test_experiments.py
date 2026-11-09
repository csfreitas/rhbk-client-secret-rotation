"""Harness unit tests only. Passing these is never RHBK/CRC integration evidence."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

FILE = Path(__file__).resolve().parents[1]/"integration"/"post_expiration.py"
spec = importlib.util.spec_from_file_location("post_expiration", FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Clock:
    def __init__(self): self.now = 1900000000.0
    def time(self): return self.now


class FakeAPI:
    """Only tests measurement logic; not an emulation proving Keycloak behavior."""
    def __init__(self, clock, renew=True, old_capped=False):
        self.clock, self.renew, self.old_capped = clock, renew, old_capped
        self.post_calls, self.creations = 0, 0
    def create(self, scenario, policy=True):
        self.creations += 1
        self.policy, self.original = policy, self.clock.now+300
        self.initial = self.clock.now
        self.current, self.old = "SENSITIVE_S1", None
        self.e2, self.rotated_exp = (self.original if policy else None), None
        return "test-client", "uuid", self.current, {"start": self.initial, "end": self.initial}, self.get_meta("uuid")
    def get_meta(self, cid):
        return {"created": self.clock.now, "expires": self.e2,
                "rotated_created": self.clock.now if self.old else None, "rotated_expires": self.rotated_exp}
    def rotate(self, cid):
        self.post_calls += 1
        self.old, self.current = self.current, "SENSITIVE_S2"
        if self.policy:
            self.e2 = self.clock.now+300 if self.renew else self.original
            self.rotated_exp = min(self.original, self.clock.now+120) if self.old_capped else self.clock.now+120
        return self.current, {"start": self.clock.now, "end": self.clock.now}
    def auth(self, cid, secret, keep_token=False):
        ok = ((secret == self.current and (self.e2 is None or self.clock.now <= self.e2)) or
              (secret == self.old and self.policy and self.clock.now <= self.rotated_exp))
        return {"http": 200 if ok else 401, "oauth_error": None if ok else "invalid_client",
                "token_received": ok, "category": "http", "timestamp": module.utc(self.clock.now), "epoch": self.clock.now}
    def secret(self, cid): return self.current


class ExperimentTests(unittest.TestCase):
    def test_allowlist_excludes_all_credential_values(self):
        original = {"secret": "SUPERSECRET", "attributes": {
            "client.secret.rotated": "OLD_SECRET", "unrelated": "TOKEN",
            "client.secret.expiration.time": "1900000000000",
            "client.secret.creation.time": "1899999700"}}
        result = module.metadata(original)
        self.assertEqual(result["expires"], 1900000000)
        self.assertEqual(result["created"], 1899999700)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("TOKEN", json.dumps(result))

    def test_oauth_credential_rejection_requires_expected_http(self):
        for result in ({"http": 0, "oauth_error": "invalid_client"},
                       {"http": 403, "oauth_error": "invalid_client"},
                       {"http": 401, "oauth_error": "invalid_grant"},
                       {"http": 503, "oauth_error": "invalid_client"}):
            self.assertFalse(module.invalid_auth(result))
        self.assertTrue(module.invalid_auth({"http": 401, "oauth_error": "invalid_client"}))
        self.assertTrue(module.invalid_auth({"http": 401, "oauth_error": "unauthorized_client"}))

    def test_errors_do_not_copy_server_descriptions(self):
        self.assertEqual(module.oauth_error({"error": "invalid_client", "error_description": "leaked secret"}), "invalid_client")
        self.assertEqual(module.oauth_error({"error": "secret-derived-arbitrary-value"}), "other_oauth_error")
        self.assertEqual(module.classify_error(urllib.error.URLError(ssl.SSLError("private"))), "tls_error")

    def test_non_crc_url_rejected_before_any_oc(self):
        with patch.object(module, "oc") as oc:
            with self.assertRaises(module.Blocked): module.guard("https://production.example", "lab-secret-rotation")
            oc.assert_not_called()

    def test_clock_units_invalid_values(self):
        self.assertEqual(module.epoch_seconds("1900000000"), 1900000000)
        self.assertEqual(module.epoch_seconds("1900000000000"), 1900000000)
        with self.assertRaises(module.Blocked): module.epoch_seconds("secret")
        with self.assertRaises(module.Blocked): module.epoch_seconds(-3)

    def run_experiment(self, scenario="early", renew=True, old_capped=False):
        clock = Clock()
        api = FakeAPI(clock, renew, old_capped)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), patch.object(module.time, "time", clock.time):
            ev = module.Evidence(directory, "TEST_ONLY")
            experiment = module.Experiment(api, ev)
            experiment.wait = lambda deadline: setattr(clock, "now", max(clock.now, deadline))
            result = experiment.expiration(scenario)
            text = ev.path.read_text()
            ev.finish()
        return result, ev.events, api, text

    def test_success_requires_auth_after_original_before_new_expiry(self):
        result, events, api, text = self.run_experiment()
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(api.post_calls, 1)
        events = {e["event"]: e for e in events}
        self.assertEqual(events["S2_beyond_original_expiration"]["http"], 200)
        self.assertEqual(events["S2_after_own_expiration"]["http"], 401)
        self.assertNotIn("SENSITIVE", text)

    def test_nonrenewal_is_not_reported_pass_after_http_success(self):
        result, events, api, _ = self.run_experiment(renew=False)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(api.post_calls, 1)
        self.assertTrue(any(e["event"] == "E2_matches_POST_plus_TTL" and e["result"] == "FAIL" for e in events))

    def test_near_expiration_distinguishes_capped_old_secret(self):
        result, events, _, _ = self.run_experiment(scenario="near", old_capped=True)
        self.assertEqual(result["result"], "PASS")
        rule = next(e for e in events if e["event"] == "old_secret_expiration_rule")
        self.assertEqual(rule["result"], "limited_by_original")
        self.assertTrue(any(e["event"] == "S1_after_original_expiration" and e["http"] == 401 for e in events))

    def test_network_failure_is_blocked_not_expiration(self):
        clock = Clock(); api = FakeAPI(clock)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            ev = module.Evidence(directory, "TEST_ONLY")
            experiment = module.Experiment(api, ev)
            experiment.scenario, experiment.client_id = "early", "uuid"
            experiment.checks = []
            api.auth = lambda *args: {"http": 0, "category": "tls_error"}
            with self.assertRaises(module.Blocked): experiment.auth("after_expiry", "S1", "SECRET", "invalid")
            self.assertFalse(module.invalid_auth(ev.events[-1]))

    def test_redirect_cannot_forward_authorization(self):
        import email.message
        import urllib.response
        calls = []
        class FakeHTTPS(module.urllib.request.HTTPSHandler):
            def https_open(self, req):
                calls.append(req.full_url)
                headers = email.message.Message()
                headers['Location'] = 'https://outside.invalid/collect'
                response = urllib.response.addinfourl(io.BytesIO(b'{}'), headers, req.full_url, code=302)
                response.msg = 'Found'
                return response
        handler = FakeHTTPS()
        transport = module.Transport(None)
        with patch.object(module.urllib.request, 'HTTPSHandler', FakeHTTPS):
            code, body, info = transport.request('GET', 'https://rhbk.apps-crc.testing/admin/realms', token='PRIVATE_TOKEN')
        self.assertEqual(code, 302)
        self.assertEqual(calls, ['https://rhbk.apps-crc.testing/admin/realms'])

    def test_lost_post_not_retried(self):
        api = module.API.__new__(module.API)
        api.url, api.base = "https://rhbk.apps-crc.testing", "/admin/realms/lab-secret-rotation"
        api.access = lambda: "TOKEN"
        class FailedTransport:
            calls = 0
            def request(self, *args):
                self.calls += 1
                return 0, {}, {"category": "timeout"}
        api.http = FailedTransport()
        with self.assertRaisesRegex(module.Blocked, "mutation_outcome_unknown_no_retry"):
            api.rotate("uuid")
        self.assertEqual(api.http.calls, 1)

class BootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        scripts = str(FILE.parents[2]/'scripts')
        sys.path.insert(0, scripts)
        spec = importlib.util.spec_from_file_location('lab_bootstrap_test', Path(scripts)/'bootstrap.py')
        cls.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bootstrap)

    def test_bootstrap_requires_exact_build_from_public_lock(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.bootstrap,'ROOT',Path(directory)):
            lock=Path(directory)/'versions.lock.yaml'
            lock.write_text(json.dumps({'expected_server_version':'26.6.9.redhat-00002'}))
            self.bootstrap.verify_server_version('26.6.9.redhat-00002')
            for actual in ('26.6.90.redhat-00002','26.6.9.redhat-00001','26.6.9'):
                with self.assertRaisesRegex(RuntimeError,'differs from versions.lock'):
                    self.bootstrap.verify_server_version(actual)
            lock.write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'missing from versions.lock'):
                self.bootstrap.verify_server_version('26.6.9.redhat-00002')

    def image_fixture(self, root):
        images={}
        (root/'deploy/workloads').mkdir(parents=True)
        for app,container in [('m2m-probe','probe'),('rotation-controller','controller')]:
            digest='sha256:'+('a' if app=='m2m-probe' else 'b')*64
            images[app]={'digest':digest}
            template={'spec':{'containers':[{'name':container,'image':'image-registry.openshift-image-registry.svc:5000/'+self.bootstrap.NS+'/'+app+'@'+digest}]}}
            spec={'template':template} if app=='m2m-probe' else {'jobTemplate':{'spec':{'template':template}}}
            (root/'deploy/workloads'/(app+'.json')).write_text(json.dumps({'spec':spec}))
        (root/'versions.lock.yaml').write_text(json.dumps({'application_images':images}))

    def test_bootstrap_requires_both_builds_before_cluster_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.bootstrap,'ROOT',Path(directory)), patch.object(self.bootstrap,'guard') as guard, patch.object(self.bootstrap.sys,'argv',['bootstrap.py']):
            (Path(directory)/'versions.lock.yaml').write_text(json.dumps({'application_images':{}}))
            with self.assertRaisesRegex(RuntimeError,'run make build'):self.bootstrap.main()
            guard.assert_not_called()

    def test_bootstrap_accepts_matching_built_images_and_rejects_placeholder(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(self.bootstrap,'ROOT',Path(directory)):
            root=Path(directory);self.image_fixture(root)
            self.bootstrap.verify_workload_images()
            file=root/'deploy/workloads/m2m-probe.json';manifest=json.loads(file.read_text())
            manifest['spec']['template']['spec']['containers'][0]['image']='m2m-probe:build-required'
            file.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError,'run make build'):self.bootstrap.verify_workload_images()

    def test_realm_only_does_not_require_workload_builds(self):
        with patch.object(self.bootstrap,'verify_workload_images') as verify, patch.object(self.bootstrap,'guard',side_effect=RuntimeError('unit guard stop')), patch.object(self.bootstrap.sys,'argv',['bootstrap.py','--realm-only']):
            with self.assertRaisesRegex(RuntimeError,'unit guard stop'):self.bootstrap.main()
            verify.assert_not_called()

    def test_feature_list_is_not_confused_with_active_flag(self):
        profile = {'previewFeatures': ['CLIENT_SECRET_ROTATION'], 'disabledFeatures': ['CLIENT_SECRET_ROTATION']}
        self.assertFalse(self.bootstrap.feature_enabled(profile))
        self.assertTrue(self.bootstrap.feature_enabled(profile, [{'name':'CLIENT_SECRET_ROTATION','enabled':True}]))
        self.assertFalse(self.bootstrap.feature_enabled({}, [{'name':'CLIENT_SECRET_ROTATION','enabled':False}]))

    def test_foreign_bootstrap_objects_are_rejected(self):
        with self.assertRaises(RuntimeError): self.bootstrap.owned({'metadata': {'labels': {'lab.rhbk/owner': 'someone-else'}}})
        self.assertIsNone(self.bootstrap.owned(None))

    def test_readback_accepts_default_fields_but_rejects_broad_policy(self):
        expected = {'policies':[{'name':'lab', 'enabled':True,
                    'conditions':[{'condition':'client-roles','configuration':{'roles':['lab-secret-rotation']}}],
                    'profiles':['lab-profile']}]}
        actual = json.loads(json.dumps(expected))
        actual['policies'][0]['conditions'][0]['configuration']['is-negative-logic'] = False
        self.bootstrap.verify_model(actual, expected, 'policies')
        actual['policies'][0]['conditions'][0]['configuration']['is-negative-logic'] = True
        with self.assertRaises(RuntimeError): self.bootstrap.verify_model(actual, expected, 'policies')
        actual['policies'][0]['conditions'] = []
        with self.assertRaises(RuntimeError): self.bootstrap.verify_model(actual, expected, 'policies')


class NamespaceGuardTests(unittest.TestCase):
    def test_same_cluster_and_labels_cannot_adopt_another_checkout_namespace(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module,'ROOT',Path(directory)):
            evidence=Path(directory)/'evidence';evidence.mkdir()
            (evidence/'cluster-identity.json').write_text(json.dumps({'server':'https://api.crc.testing:6443','uid':'cluster-test'}))
            objects=[{'clusters':[{'cluster':{'server':'https://api.crc.testing:6443'}}]},
                     {'metadata':{'uid':'cluster-test'}},
                     {'metadata':{'uid':'namespace-test','labels':{'lab.rhbk/owner':module.OWNER}}}]
            with patch.object(module,'oc',side_effect=objects) as read:
                with self.assertRaisesRegex(module.Blocked,'namespace_identity_missing'):
                    module.guard('https://rhbk-rotation.apps-crc.testing','lab-secret-rotation')
                self.assertEqual(read.call_count,3)
            (evidence/'namespace-identity.json').write_text(json.dumps({'name':module.NAMESPACE,'uid':'other','cluster_uid':'cluster-test'}))
            with patch.object(module,'oc',side_effect=objects) as read:
                with self.assertRaisesRegex(module.Blocked,'not_created_by_this_checkout'):
                    module.guard('https://rhbk-rotation.apps-crc.testing','lab-secret-rotation')
                self.assertEqual(read.call_count,3)
            (evidence/'namespace-identity.json').write_text(json.dumps({'name':module.NAMESPACE,'uid':'namespace-test','cluster_uid':'cluster-test'}))
            with patch.object(module,'oc',side_effect=objects+[{'spec':{'suspend':True}},{'items':[]}]):
                self.assertEqual(module.guard('https://rhbk-rotation.apps-crc.testing','lab-secret-rotation')['namespace_uid'],'namespace-test')


class PodClockFallbackTests(unittest.TestCase):
    def objects(self, foreign=False):
        return [
            {'metadata': {'labels': {'lab.rhbk/owner': module.OWNER}}},
            {'metadata': {'name':'rhbk-0','uid':'pod-uid','ownerReferences':[{'kind':'StatefulSet','name':'rhbk','uid':'wrong' if foreign else 'sts-uid'}]},
             'spec': {'containers':[{'name':'keycloak','image':'registry/lab/rhbk@sha256:digest'}]}},
            {'metadata': {'name':'rhbk','uid':'sts-uid','ownerReferences':[{'kind':'Keycloak','name':'rhbk','uid':'cr-uid'}]}},
            {'metadata': {'name':'rhbk','uid':'cr-uid','labels':{'lab.rhbk/owner':module.OWNER}},
             'spec': {'image':'registry/lab/rhbk@sha256:digest'}}]

    def test_clock_reads_owned_pod_and_records_bounded_offset(self):
        with patch.object(module,'oc',side_effect=self.objects()), patch.object(module,'oc_raw',return_value='1900000000\n') as read, patch.object(module.time,'time',side_effect=[1900000000.1,1900000000.3]), patch.object(module.time,'monotonic',side_effect=[10,10.2]):
            result = module.pod_clock_sample('26.6.9.redhat-00002',5)
        self.assertEqual(result['source'],'RHBK_pod_OS_clock')
        self.assertEqual(result['offset_lower_seconds'],-0.3)
        self.assertEqual(result['offset_upper_seconds'],0.9)
        self.assertEqual(result['server_offset_seconds'],0.3)
        self.assertTrue(result['ownership_chain_verified'])
        self.assertEqual(read.call_args[0][0],['-n','rhbk-rotation-lab','exec','rhbk-0','-c','keycloak','--','date','-u','+%s'])

    def test_foreign_owner_chain_never_executes_pod_command(self):
        with patch.object(module,'oc',side_effect=self.objects(True)), patch.object(module,'oc_raw') as read:
            with self.assertRaisesRegex(module.Blocked,'owner_chain_not_verified'):
                module.pod_clock_sample('26.6.9.redhat-00002',5)
        read.assert_not_called()

    def test_pod_skew_is_blocked(self):
        with patch.object(module,'oc',side_effect=self.objects()), patch.object(module,'oc_raw',return_value='1900000100\n'), patch.object(module.time,'time',side_effect=[1900000000.1,1900000000.3]), patch.object(module.time,'monotonic',side_effect=[10,10.2]):
            with self.assertRaisesRegex(module.Blocked,'offset_or_uncertainty'):
                module.pod_clock_sample('26.6.9.redhat-00002',5)

    def test_unmeasured_runtime_never_executes_pod_command(self):
        with patch.object(module,'oc') as read:
            with self.assertRaises(module.Blocked): module.pod_clock_sample('NOT_MEASURED',5)
        read.assert_not_called()


class TokenVsSecretTests(unittest.TestCase):
    class API(FakeAPI):
        def __init__(self, clock, later_active=True):
            super().__init__(clock)
            self.url, self.token_path = 'https://rhbk.apps-crc.testing', '/realms/lab-secret-rotation/protocol/openid-connect/token'
            self.mapper, self.introspections, self.later_active = None, 0, later_active
            self.http = self
        def admin(self, method, path, body=None):
            if method == 'POST' and path.endswith('/protocol-mappers/models'):
                self.mapper = body
                return {}, {}
            if method == 'GET' and path.endswith('/protocol-mappers/models'):
                return [self.mapper], {}
            raise AssertionError('unexpected admin operation')
        def auth(self, client_id, secret, keep_token=False):
            result = super().auth(client_id, secret)
            if keep_token:
                if self.mapper is None: raise AssertionError('retained token issued before mapper')
                return result, 'SENSITIVE_RETAINED_TOKEN'
            return result
        def request(self, method, url, body, form=False):
            if method != 'POST' or not url.endswith('/introspect'): raise AssertionError('unexpected HTTP operation')
            self.introspections += 1
            return 200, {'active': True if self.introspections == 1 else self.later_active,
                         'exp': self.initial+600}, {'category': 'http'}

    def run_token(self, later_active=True):
        clock = Clock(); api = self.API(clock, later_active)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), patch.object(module.time,'time',clock.time):
            ev = module.Evidence(directory,'TEST_ONLY')
            experiment = module.Experiment(api,ev)
            experiment.wait = lambda deadline: setattr(clock,'now',max(clock.now,deadline))
            result = experiment.token_vs_secret()
            output = ev.path.read_text()
        return result, ev.events, api, output

    def test_active_token_after_old_secret_rejection_is_required(self):
        result, events, api, output = self.run_token(True)
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(api.introspections,2)
        self.assertEqual(api.post_calls,1)
        self.assertEqual(api.mapper['config']['included.client.audience'],'test-client')
        self.assertTrue(any(event['event']=='new_auth_with_expired_S1' and event['http']==401 for event in events))
        self.assertTrue(any(event['event']=='previously_issued_token_after_S1_expiration' and event['active'] for event in events))
        self.assertNotIn('SENSITIVE',output)

    def test_http200_inactive_token_is_a_failure(self):
        result, events, _, _ = self.run_token(False)
        self.assertEqual(result['result'],'FAIL')
        self.assertIn('not confirmed',result['note'])
        self.assertTrue(any(event['event']=='previously_issued_token_after_S1_expiration' and event['result']=='FAIL' for event in events))


class PutComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('put_comparison_test', FILE.with_name('put_comparison.py'))
        cls.put = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.put)

    class API(FakeAPI):
        def __init__(self, clock, renew=False):
            super().__init__(clock, renew)
            self.put_calls = 0
            self.payload = None
        def admin(self, method, path, body=None):
            if method != 'PUT' or path != '/clients/uuid': raise AssertionError('unexpected operation')
            self.put_calls += 1
            self.payload = body
            self.current, self.old = body['secret'], None
            if self.policy and self.renew: self.e2 = self.clock.now+300
            return {}, {'start':self.clock.now,'end':self.clock.now}

    def run_comparison(self, scenario, renew=False):
        clock = Clock(); api = self.API(clock,renew)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), patch.object(module.time,'time',clock.time), patch.object(self.put.secrets,'token_urlsafe',return_value='SENSITIVE_EXTERNAL_VALUE'):
            ev = self.put.Evidence(directory,'TEST_ONLY')
            comparison = self.put.Comparison(api,ev)
            comparison.wait = lambda deadline: setattr(clock,'now',max(clock.now,deadline))
            result = comparison.run(scenario)
            ev.finish()
            output = ev.path.read_text()
            report = (Path(directory)/'report.md').read_text()
            csv = ev.path.with_suffix('.csv').read_text()
        return result, ev.events, api, output, report, csv

    def test_put_only_sends_external_secret_without_internal_attributes(self):
        result, events, api, output, report, csv = self.run_comparison('policy-before')
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(set(api.payload),{'secret'})
        self.assertEqual(api.put_calls,1)
        self.assertEqual(api.post_calls,0)
        self.assertNotIn('SENSITIVE',output)
        self.assertIn('após PUT',report)
        self.assertIn('expiration_after_put',csv)
        self.assertNotIn('expiration_after_post',csv)

    def test_after_policy_observes_nonrenewal_without_claiming_equivalence(self):
        result, events, _, _, _, _ = self.run_comparison('policy-after',renew=False)
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(result['current_auth_http'],401)
        self.assertEqual(result['original_expiration'],result['expiration_after_put'])
        self.assertIn('no equivalence',result['note'])
        self.assertTrue(any(item['event']=='S1_before_PUT_after_wait' and item['http']==401 for item in events))

    def test_after_policy_can_record_renewal_if_server_exhibits_it(self):
        result, _, _, _, _, _ = self.run_comparison('policy-after',renew=True)
        self.assertEqual(result['current_auth_http'],200)
        self.assertGreater(result['expiration_after_put'],result['original_expiration'])

    def test_control_after_reference_does_not_invent_expiration(self):
        result, events, _, _, _, _ = self.run_comparison('control-after')
        self.assertEqual(result['result'],'PASS')
        self.assertIsNone(result['original_expiration'])
        self.assertIsNone(result['expiration_after_put'])
        event = next(item for item in events if item['event']=='wait_for_precondition')
        self.assertIsNone(event['server_deadline'])
        self.assertIsNotNone(event['elapsed_reference_deadline'])


class EvidenceReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('review_evidence_test',FILE.with_name('review_post_evidence.py'))
        cls.review = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.review)

    def original_events(self):
        _, events, _, _ = ExperimentTests().run_experiment()
        for event in events:
            if event.get('event') in ('S1_after_rotated_expiration','S2_after_own_expiration'):
                event['oauth_error']='unauthorized_client'
                event['result']='FAIL'
            elif event.get('event')=='conclusion':
                event['result']='FAIL'
        return events

    def test_review_accepts_observed_code_only_with_original_controls(self):
        events = self.original_events()
        result = self.review.review_scenario(events,'early')
        self.assertEqual(result['original_result'],'FAIL')
        self.assertEqual(result['result'],'PASS')
        self.assertEqual(len(result['corrections']),2)
        self.assertTrue(all(item['oauth_error']=='unauthorized_client' for item in result['corrections']))

    def test_missing_same_client_positive_control_prevents_reclassification(self):
        events = [event for event in self.original_events() if event['event']!='S2_after_old_expiration']
        result = self.review.review_scenario(events,'early')
        self.assertEqual(result['result'],'BLOCKED')
        self.assertFalse(any(item['event']=='S1_after_rotated_expiration' for item in result['corrections']))

    def test_network_failure_is_never_reclassified_as_expiration(self):
        events = self.original_events()
        for event in events:
            if event['event']=='S1_after_rotated_expiration':
                event['http'],event['category']=0,'network_error'
        result = self.review.review_scenario(events,'early')
        self.assertEqual(result['result'],'FAIL')
        self.assertFalse(any(item['event']=='S1_after_rotated_expiration' for item in result['corrections']))

    def test_raw_file_unchanged_and_missing_scenarios_remain_blocked(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'original.jsonl'; raw=''.join(json.dumps(e)+'\n' for e in self.original_events()).encode()
            source.write_bytes(raw)
            result=self.review.review_file(source,Path(directory)/'review')
            self.assertEqual(source.read_bytes(),raw)
            self.assertEqual(result['source_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertFalse(result['raw_modified'])
            self.assertEqual(result['result'],'BLOCKED')
            self.assertEqual(next(r for r in result['results'] if r['scenario']=='near')['result'],'BLOCKED')


if __name__ == '__main__':
    unittest.main()
