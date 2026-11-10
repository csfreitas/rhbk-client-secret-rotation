"""Portable deployment boundaries. No cluster or RHBK request is performed."""
import copy
import importlib.util
import json
import secrets
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('portable_openshift', ROOT/'scripts/openshift.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


def config():
    return {'kubeconfig': '/tmp/fixture.kubeconfig', 'context': 'explicit',
            'expected_server': 'https://api.fixture.invalid:6443', 'namespace': 'rotation-fixture',
            'architecture': 'amd64', 'rhbk_url': 'https://sso.fixture.invalid', 'realm': 'demo',
            'client_id': 'orders-service', 'admin_client_id': 'rotation-admin',
            'expected_server_version': '26.6.9.redhat-00002', 'ca_file': '/tmp/fixture.crt',
            'admin_secret_file': '/tmp/admin.secret', 'initial_secret_file': '/tmp/initial.secret',
            'images': {'probe': 'registry.invalid/team/probe@sha256:'+'a'*64,
                       'controller': 'registry.invalid/team/controller@sha256:'+'b'*64}}


class FakeBackend(portable.Backend):
    def __init__(self, cfg=None):
        super().__init__(cfg or config())
        self.record = {'server': self.CONFIG['expected_server'], 'cluster_uid': 'cluster-uid',
                       'namespace': self.NS, 'namespace_uid': 'namespace-uid', 'installation_id': 'installation-id'}
        self.objects = {('namespace', self.NS): {'metadata': {'uid': 'namespace-uid', 'labels': self.labels()}}}
        self.calls = []

    def identity(self): return self.record
    def cluster(self): return 'cluster-uid'
    def get(self, kind, name=None, ns=portable.UNSET): return copy.deepcopy(self.objects.get((kind.lower(), name)))
    def oc(self, *args, data=None, check=True):
        self.calls.append((args, copy.deepcopy(data)))
        if args[:2] == ('create', '-f'):
            value = copy.deepcopy(data); value.setdefault('metadata', {}).setdefault('uid', 'new-uid')
            self.objects[(value['kind'].lower(), value['metadata']['name'])] = value
        return ''
    def save(self, path, obj): pass


class ConfigurationBoundaries(unittest.TestCase):
    def test_no_ambient_context_or_insecure_url(self):
        for key, bad in [('context', ''), ('namespace', 'openshift-system'),
                         ('expected_server', 'http://api.invalid'), ('rhbk_url', 'https://user:password@sso.invalid'),
                         ('architecture', 'ppc64le'), ('client_id', ' leading-space')]:
            value = config(); value[key] = bad
            with self.subTest(key=key), self.assertRaises(portable.Blocked): portable.validate_config(value)

    def test_images_require_digest_not_tag(self):
        value = config(); value['images']['probe'] = 'registry.invalid/probe:latest'
        with self.assertRaisesRegex(portable.Blocked, 'immutable'): portable.validate_config(value)

    def test_zero_remaining_and_300_interval_are_supported(self):
        value = portable.validate_config(config())
        self.assertEqual(value['rotation']['remaining_window'], 0)
        self.assertEqual(value['rotation']['interval'], 300)

    def test_bad_overlap_and_principal_budgets_block(self):
        for timing in [{'rotated_ttl': 119}, {'interval': 120}, {'principal_ttl': 350}]:
            value = config(); value['rotation'] = timing
            with self.subTest(timing=timing), self.assertRaises(portable.Blocked): portable.validate_config(value)

    def test_credentials_are_private_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'value'; path.write_text('unit-fixture'); path.chmod(0o644)
            with self.assertRaises(portable.Blocked): portable.credential(path)
            path.chmod(0o600); self.assertEqual(portable.credential(path), 'unit-fixture')
            link = Path(directory)/'link'; link.symlink_to(path)
            with self.assertRaises(portable.Blocked): portable.credential(link)

    def test_private_write_is_atomic_and_restricts_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'identity.json'
            portable.private_write(path, '{"installation_id":"first"}')
            with patch.object(portable.os, 'replace', side_effect=OSError('simulated replacement failure')):
                with self.assertRaises(OSError): portable.private_write(path, '{"installation_id":"second"}')
            self.assertEqual(json.loads(path.read_text())['installation_id'],'first')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_oc_never_falls_back_to_ambient_context(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(portable.subprocess, 'run') as run:
            path = Path(directory)/'missing'
            with self.assertRaises(portable.Blocked): portable.run_oc(path, 'explicit', ['get', 'pods'])
            run.assert_not_called()

    def test_proxy_uses_zero_api_timeout_but_bounded_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fixture.kubeconfig'; path.write_text('fixture')
            result = type('Result', (), {'returncode':0,'stdout':b'{}'})()
            with patch.object(portable.subprocess, 'run', return_value=result) as run:
                portable.run_oc(path, 'explicit', ['get','--raw','/api/v1/namespaces/demo/pods/p:8080/proxy/status'])
                self.assertIn('--request-timeout=0', run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['timeout'],40)
                self.assertIn('--context=explicit',run.call_args.args[0])
                portable.run_oc(path, 'explicit', ['get','pods'])
                self.assertIn('--request-timeout=30s',run.call_args.args[0])

    def test_configure_rejects_existing_namespace_before_credentials_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'; path.write_text(json.dumps(config()))
            backend = FakeBackend()
            with patch.object(portable, 'PRIVATE', Path(directory)/'.private'), patch.object(portable, 'read_file'), \
                 patch.object(portable, 'credential'), patch.object(portable.ssl, 'create_default_context'), \
                 patch.object(portable, 'Backend', return_value=backend), patch.object(portable, 'verify_rhbk') as verify:
                with self.assertRaisesRegex(portable.Blocked, 'namespace already exists'): portable.configure(path)
                verify.assert_not_called()
                self.assertFalse((Path(directory)/'.private').exists())


class OwnershipAndRender(unittest.TestCase):
    def test_guard_checks_cluster_namespace_and_installation(self):
        for field, bad in [('cluster_uid', 'another-cluster'), ('namespace_uid', 'another-namespace'),
                           ('installation_id', 'another-installation')]:
            backend = FakeBackend(); backend.record[field] = bad
            with self.subTest(field=field), self.assertRaises(portable.Blocked): backend.guard()

    def test_missing_recorded_namespace_cannot_be_recreated(self):
        backend = FakeBackend(); backend.objects.clear()
        with self.assertRaises(portable.Blocked): backend.guard(require_namespace=False)

    def test_namespace_readback_after_lost_create_response_requires_intent(self):
        backend = FakeBackend(); backend.record['namespace_uid'] = None
        with self.assertRaises(portable.Blocked): backend.guard(require_namespace=False, allow_pending_namespace=True)
        backend.record['namespace_create_intent'] = backend.record['installation_id']
        self.assertIsNotNone(backend.guard(require_namespace=False, allow_pending_namespace=True))
        with self.assertRaises(portable.Blocked): backend.guard()
        with patch.object(portable, 'private_write') as write:
            portable.ensure_namespace(backend)
        persisted = json.loads(write.call_args.args[1])
        self.assertEqual(persisted['namespace_uid'],'namespace-uid')
        self.assertEqual(backend.calls,[])

    def test_render_is_namespaced_and_has_no_platform_or_secret_values(self):
        backend = FakeBackend(); cfg = backend.CONFIG
        cfg.update(secret_prefix='orders-cred', cronjob_name='orders-rotator', state_name='orders-state',
                   deployment_name='orders-probe', lease_name='orders-lease', architecture='arm64')
        result = portable.render(backend)
        self.assertEqual(backend.calls, [])
        self.assertFalse({'Namespace','ClusterRole','ClusterRoleBinding','Keycloak','Secret'} & {x['kind'] for x in result['items']})
        self.assertTrue(all(x['metadata']['namespace'] == backend.NS for x in result['items']))
        cron = next(x for x in result['items'] if x['kind'] == 'CronJob')
        self.assertEqual(cron['metadata']['name'], 'orders-rotator'); self.assertTrue(cron['spec']['suspend'])
        pod = cron['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector'], {'kubernetes.io/arch':'arm64'})
        env = {v['name']:v.get('value') for v in pod['containers'][0]['env']}
        self.assertEqual(env['SECRET_PREFIX'], 'orders-cred'); self.assertEqual(env['TARGET_CLIENT_ID'], 'orders-service')
        self.assertEqual(env['ROTATE_INTERVAL'], '300')
        self.assertNotIn('apps-crc.testing', json.dumps(result))
        self.assertNotIn('catalogo-api', json.dumps(result))
        deployment = next(x for x in result['items'] if x['kind'] == 'Deployment')
        self.assertEqual(deployment['metadata']['name'], 'orders-probe')
        references = [x['valueFrom']['secretKeyRef']['name'] for x in deployment['spec']['template']['spec']['containers'][0]['env']
                      if x.get('valueFrom', {}).get('secretKeyRef')]
        self.assertEqual(set(references), {'orders-cred-v0001'})

    def test_existing_generation_is_not_overwritten(self):
        backend = FakeBackend()
        backend.objects[('configmap', backend.STATE_NAME)] = {
            'metadata': backend.meta(backend.STATE_NAME), 'data': {'state.json': json.dumps({'phase':'COMPLETE','generation':7,'suspended':True})}}
        with patch.object(portable, 'verify_rhbk') as verify:
            with self.assertRaisesRegex(portable.Blocked, 'already ran'): portable.apply(backend)
            verify.assert_not_called()
        self.assertEqual(backend.calls, [])

    def test_initial_import_creates_consistent_generation_and_is_retryable(self):
        backend = FakeBackend()
        metadata = {portable.META[0]:1000000,portable.META[1]:1086400}
        verification = {'client_uuid':'client-uuid','metadata':metadata,'auth_http':200,'server_version':backend.CONFIG['expected_server_version']}
        with patch.object(portable,'verify_rhbk',return_value=verification), patch.object(portable,'credential',return_value='unit-fixture'), \
             patch.object(portable,'read_file',return_value=b'public-ca'):
            portable.apply(backend)
            count = len(backend.calls)
            portable.apply(backend)
        self.assertEqual(len(backend.calls),count)
        state = json.loads(backend.objects[('configmap',backend.STATE_NAME)]['data']['state.json'])
        self.assertEqual(state['generation'],1); self.assertTrue(state['suspended'])
        self.assertEqual(state['target_client_id'],'orders-service')
        self.assertEqual(state['current_secret'],'m2m-credential-v0001')
        self.assertTrue(backend.objects[('cronjob',backend.CRONJOB_NAME)]['spec']['suspend'])
        self.assertTrue(backend.objects[('secret','m2m-credential-v0001')]['immutable'])

    def test_apply_unknown_existing_resource_is_refused(self):
        backend = FakeBackend(); backend.objects[('secret','existing')] = {'metadata':{'labels':{}}}
        obj = portable.secret_object(backend, 'existing', {'client-secret':secrets.token_urlsafe(24)})
        with self.assertRaisesRegex(portable.Blocked, 'another installation'): portable.create_resource(backend, obj)
        self.assertEqual(backend.calls, [])

    def test_existing_owned_cron_with_changed_suspend_or_env_is_not_accepted(self):
        backend = FakeBackend()
        desired = next(x for x in portable.render(backend)['items'] if x['kind']=='CronJob')
        for change in ('suspend','env'):
            actual = copy.deepcopy(desired)
            if change=='suspend': actual['spec']['suspend']=False
            else: actual['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env'][0]['value']='https://other.invalid'
            backend.objects[('cronjob',desired['metadata']['name'])]=actual
            with self.subTest(change=change), self.assertRaisesRegex(portable.Blocked,'managed fields'):
                portable.create_resource(backend,desired)
        self.assertEqual(backend.calls,[])

    def test_evidence_is_separate_from_crc_and_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(portable,'ROOT',Path(directory)):
            backend = FakeBackend()
            portable.Backend.save(backend,'evidence/baseline.json',{'test':True})
            self.assertTrue((Path(directory)/'evidence/openshift/baseline.json').is_file())
            self.assertFalse((Path(directory)/'evidence/baseline.json').exists())
            for path in ['../escape.json','/tmp/escape.json','evidence/../escape.json']:
                with self.assertRaises(portable.Blocked): portable.Backend.save(backend,path,{})

    def test_backend_restricts_shared_control_mutations_to_owned_jobs(self):
        backend = FakeBackend()
        with self.assertRaises(portable.Blocked): backend.apply({'kind':'Deployment','metadata':backend.meta('m2m-probe')})
        self.assertEqual(backend.calls, [])


class ReadOnlyRHBK(unittest.TestCase):
    def fixture(self, missing_metadata=False, expired=False, other_policy=False, hidden_version=False):
        cfg = portable.validate_config(config()); now = 1000000
        expected = {'expiration-period':86400, 'rotated-expiration-period':120, 'remaining-rotation-period':0}
        attrs = {} if missing_metadata else {portable.META[0]:str(now-1000), portable.META[1]:str(now-1000+86400)}
        if expired: attrs[portable.META[1]] = str(now-1)
        clients = {'admin':{'id':'admin','clientId':'rotation-admin','attributes':{}},
                   'target':{'id':'target','clientId':'orders-service','enabled':True,'publicClient':False,
                             'serviceAccountsEnabled':True,'clientAuthenticatorType':'client-secret','attributes':attrs}}
        requests = []
        class HTTP:
            def __init__(self,*args): pass
            def request(self, method, path, body=None, **kwargs):
                requests.append((method,path))
                if method == 'POST': return 200, {'access_token':'unit-token'}, {}
                if path == '/admin/serverinfo': value={'systemInfo':{} if hidden_version else {'version':cfg['expected_server_version']},'features':[{'name':'client-secret-rotation','enabled':True}]}
                elif path.endswith('/client-policies/profiles'): value={'profiles':[{'name':cfg['profile_name'],'executors':[{'executor':'secret-rotation','configuration':expected}]}]}
                elif path.endswith('/client-policies/policies'):
                    policy={'name':cfg['policy_name'],'enabled':True,'profiles':[cfg['profile_name']], 'conditions':[{'condition':'client-roles','configuration':{'roles':[cfg['policy_role']]}}]}
                    value={'policies':[policy, {**policy,'name':'other'}] if other_policy else [policy]}
                elif '?clientId=' in path: value=[clients['admin' if path.endswith('rotation-admin') else 'target']]
                elif path.endswith('/admin/roles'): value=[]
                elif path.endswith('/target/roles'): value=[{'name':cfg['policy_role']}]
                elif path.endswith('/client-secret'): value={'value':'unit-initial'}
                elif path.endswith('/admin'): value=clients['admin']
                elif path.endswith('/target'): value=clients['target']
                else: raise AssertionError(path)
                return 200,value,{}
        return cfg, now, HTTP, requests

    def test_verification_never_rotates_or_changes_policy(self):
        cfg, now, http, requests = self.fixture()
        module = type('Module', (), {'HTTP':http})
        with patch.object(portable, 'controller_module', return_value=module), patch.object(portable, 'credential', return_value='unit-initial'), patch.object(portable.time, 'time', return_value=now):
            result = portable.verify_rhbk(cfg)
        self.assertEqual(result['metadata'][portable.META[1]] - result['metadata'][portable.META[0]],86400)
        self.assertTrue(all(method=='GET' or path.endswith('/protocol/openid-connect/token') for method,path in requests))

    def test_missing_metadata_expired_or_ambiguous_policy_refused(self):
        for kwargs in [{'missing_metadata':True}, {'expired':True}, {'other_policy':True}]:
            cfg, now, http, _ = self.fixture(**kwargs)
            with self.subTest(kwargs=kwargs), patch.object(portable, 'controller_module', return_value=type('Module',(),{'HTTP':http})), \
                 patch.object(portable, 'credential', return_value='unit-initial'), patch.object(portable.time, 'time', return_value=now), self.assertRaises(portable.Blocked):
                portable.verify_rhbk(cfg)

    def test_hidden_server_build_reports_read_only_permission_requirement(self):
        cfg, now, http, _ = self.fixture(hidden_version=True)
        with patch.object(portable, 'controller_module', return_value=type('Module',(),{'HTTP':http})), \
             patch.object(portable, 'credential', return_value='unit-initial'), patch.object(portable.time, 'time', return_value=now), \
             self.assertRaisesRegex(portable.Blocked, 'view-system'):
            portable.verify_rhbk(cfg)


if __name__ == '__main__': unittest.main()
