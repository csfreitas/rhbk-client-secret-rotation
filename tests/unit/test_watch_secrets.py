"""Run the shell watcher against fake CLIs; never contact a cluster or server."""
import base64
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/watch-secrets.sh'
LABELS = {'app.kubernetes.io/part-of': 'rhbk-secret-rotation-lab',
          'lab.rhbk/owner': 'client-secret-rotation'}
CREATED = 'client.secret.creation.time'
EXPIRES = 'client.secret.expiration.time'
ROTATED_CREATED = 'client.secret.rotated.creation.time'
ROTATED_EXPIRES = 'client.secret.rotated.expiration.time'

STUB = r'''#!/usr/bin/env python3
import base64, json, os, pathlib, sys, time
from urllib.parse import parse_qs
folder = pathlib.Path(os.environ['WATCH_TEST_DIR'])
fixture = json.loads((folder/'fixture.json').read_text())
clock_file = folder/'started'
if not clock_file.exists(): clock_file.write_text(str(time.time()))
started = float(clock_file.read_text())
now = time.time()
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
record = {'tool': tool, 'args': args}
with (folder/'calls.jsonl').open('a') as output: output.write(json.dumps(record)+'\n')
labels = fixture['labels']
first_name = 'credential-v0001'
next_name = 'credential-v0002'
def option(name): return args[args.index(name)+1] if name in args else None
def metadata(name):
    return {'name':name,'namespace':'watch-demo','uid':'uid-'+name,'resourceVersion':'1','labels':labels}
def state(kind):
    pre = {fixture['created']: started-50, fixture['expires']: started+315360000}
    post = {fixture['created']:started+1.8, fixture['expires']:started+315360000,
            fixture['rotated_created']:started-50, fixture['rotated_expires']:started+6.2}
    result = {'phase':'IDLE','generation':1,'current_secret':first_name,'suspended':False,
              'last_rotated_at':started-50,'namespace':'watch-demo','realm':'watch-realm',
              'target_client_id':'watch-client','secret_prefix':'credential','initial_metadata':pre}
    if kind == 'idle': return result
    result.update({'cycle_id':'cycle-two','old_generation':1,'old_secret':first_name,
                   'next_generation':2,'next_secret':next_name,'pre_metadata':pre,
                   'post_started_at':started+1.8,'post_finished_at':started+1.9,
                   'post_metadata':post})
    if kind == 'pending':
        result.update({'phase':'INTENT_RECORDED','post_finished_at':None,'post_started_at':started-100,
                       'post_metadata':{fixture['created']:started-100,fixture['expires']:started+100,
                                        fixture['rotated_expires']:started-80}})
    elif kind == 'holding':
        result['phase']='HOLDING_OLD'
        result['post_started_at']=started-1
        result['post_finished_at']=started-0.9
        post[fixture['created']]=started-1
        post[fixture['rotated_expires']]=started+120
    elif kind == 'expired':
        result['phase']='COMPLETE';result['generation']=2;result['current_secret']=next_name
        result['post_started_at']=started-125;result['post_finished_at']=started-124.9
        post[fixture['created']]=started-125;post[fixture['rotated_expires']]=started-5
    elif kind == 'cycling':
        if now-started < 1.8: return state('idle')
        result['phase']='HOLDING_OLD' if now-started < 6.2 else 'VERIFYING'
    else: result['phase']='HOLDING_OLD'
    return result
if tool == 'oc':
    if 'config' in args:
        value = {'clusters':[{'name':'test','cluster':{'server':fixture.get('server','https://api.example.test:6443'),
                 'insecure-skip-tls-verify':fixture.get('insecure',False)}}],
                 'contexts':[{'name':'explicit','context':{'cluster':'test','user':'test'}}],
                 'current-context':'explicit'}
    elif 'get' not in args:
        print('unexpected mutation', file=sys.stderr);sys.exit(97)
    else:
        index=args.index('get');kind=args[index+1];name=args[index+2]
        if kind in ('namespace','namespaces','ns'):
            value={'apiVersion':'v1','kind':'Namespace','metadata':metadata(name)}
        elif kind in ('configmap','configmaps','cm'):
            count_file=folder/'state-count'
            count=int(count_file.read_text()) if count_file.exists() else 0
            count_file.write_text(str(count+1))
            value_state=state(fixture.get('state','holding'))
            if fixture.get('race') and count > 0:
                value_state['cycle_id']='another-cycle'
                value_state['post_started_at']+=60
            value={'apiVersion':'v1','kind':'ConfigMap','metadata':metadata(name),
                   'data':{'state.json':json.dumps(value_state)}}
            value['metadata']['resourceVersion']=str(2 if fixture.get('race') and count > 0 else 1)
        elif kind in ('secret','secrets'):
            if name not in ('credential-v0001','credential-v0002'):
                print('unexpected Secret lookup',file=sys.stderr);sys.exit(98)
            if name.endswith('v0002') and fixture.get('missing_new'):

                if '--ignore-not-found' in args: sys.exit(0)
                print('NotFound',file=sys.stderr);sys.exit(1)
            generation=name.rsplit('-',1)[1]
            identity=fixture.get('client_id','watch-client')
            secret=fixture['old_value'] if generation=='v0001' else fixture['new_value']
            encode=lambda value:base64.b64encode(value.encode()).decode()
            value={'apiVersion':'v1','kind':'Secret','metadata':metadata(name),'immutable':not fixture.get('mutable',False),
                   'type':'Opaque','data':{'client-id':encode(identity),'client-secret':encode(secret),'generation':encode(generation)}}
            value['metadata']['labels']={**labels,'lab.rhbk/generation':generation}
        else:
            print('unexpected read',file=sys.stderr);sys.exit(99)
    print(json.dumps(value));sys.exit(0)
if tool != 'curl': sys.exit(99)
form={}
for index, arg in enumerate(args):
    if arg=='--data-urlencode':
        item=args[index+1]
        if '@' in item and '=' not in item:
            key,path=item.split('@',1);form[key]=pathlib.Path(path).read_text()
        else:
            key,value=item.split('=',1);form[key]=value
    elif arg=='--data-binary':
        value=sys.stdin.read() if args[index+1]=='@-' else args[index+1]
        form.update({key:items[0] for key,items in parse_qs(value).items()})
if form.get('grant_type')!='client_credentials' or form.get('client_id')!='watch-client':sys.exit(95)
provided=form.get('client_secret')
old=provided==fixture['old_value']
if not old and provided!=fixture['new_value']:sys.exit(94)
if not any('/realms/watch-realm/protocol/openid-connect/token' in item for item in args):sys.exit(93)
status=200
error=None
if old:
    status=fixture.get('old_http',200)
    if fixture.get('state')=='cycling' and now-started>=6.2:status=fixture.get('post_expiry_http',401)
    if fixture.get('state')=='expired':status=fixture.get('old_http',401)
    error=fixture.get('old_error','unauthorized_client')
errno=fixture.get('old_errno',0) if old else 0
if old and fixture.get('state')=='cycling' and now-started>=6.2:errno=fixture.get('post_expiry_errno',0)
body={'access_token':fixture['token'],'expires_in':300}
if status!=200:body={'error':error,'error_description':fixture['old_value']+' '+fixture['token']}
if fixture.get('missing_token') and status==200:body={'expires_in':300}
response=option('--output') or option('-o')
if response:pathlib.Path(response).write_text('' if errno else json.dumps(body))
else:print(json.dumps(body))
if errno:
    print('transport failed',file=sys.stderr)
    print('000',end='');sys.exit(errno)
print(str(status),end='')
'''


@unittest.skipUnless(shutil.which('bash') and shutil.which('jq'), 'bash and real jq are required')
class WatchSecretsShellTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='watch-secrets-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root/'bin';self.bin.mkdir()
        self.temp = self.root/'runtime';self.temp.mkdir()
        self.calls = self.root/'calls.jsonl'
        self.report = self.root/'report.tsv'
        for tool in ('oc','curl'):
            path=self.bin/tool;path.write_text(STUB);path.chmod(0o755)
        self.fixture = {'labels':LABELS,'old_value':secrets.token_urlsafe(30),
                        'new_value':secrets.token_urlsafe(30),'token':secrets.token_urlsafe(35),
                        'created':CREATED,'expires':EXPIRES,'rotated_created':ROTATED_CREATED,
                        'rotated_expires':ROTATED_EXPIRES,'state':'holding'}
        kubeconfig=self.root/'kubeconfig';kubeconfig.write_text('{}\n');kubeconfig.chmod(0o600)
        self.config=self.root/'config.env'
        self.config.write_text('\n'.join([
            'KUBECONFIG='+str(kubeconfig),'CONTEXT=explicit','EXPECTED_SERVER=https://api.example.test:6443',
            'NAMESPACE=watch-demo','STATE_CONFIGMAP=rotation-state','RHBK_URL=https://sso.example.test',
            'REALM=watch-realm','CLIENT_ID=watch-client','INTERVAL_SECONDS=1','DURATION_SECONDS=0',
            'REPORT_FILE='+str(self.report),'WATCH_TZ=UTC'])+'\n')

    def run_watch(self, *args, expected=None):
        for name in ('calls.jsonl','started','state-count','report.tsv'):
            (self.root/name).unlink(missing_ok=True)
        path=self.root/'fixture.json';path.write_text(json.dumps(self.fixture));path.chmod(0o600)
        env={key:value for key,value in os.environ.items() if key not in {
            'KUBECONFIG','CONTEXT','EXPECTED_SERVER','NAMESPACE','STATE_CONFIGMAP','RHBK_URL',
            'REALM','CLIENT_ID','CA_FILE','INTERVAL_SECONDS','DURATION_SECONDS','REPORT_FILE','WATCH_TZ'}}
        env.update({'PATH':str(self.bin)+os.pathsep+os.environ['PATH'], 'WATCH_TEST_DIR':str(self.root),
                    'TMPDIR':str(self.temp), 'KUBECONFIG':'/should/not/use/ambient-config'})
        result=subprocess.run(['bash',str(SCRIPT),'--config',str(self.config),'--plain',*args],
                              cwd=ROOT,env=env,capture_output=True,text=True,timeout=24)
        if expected is not None:self.assertEqual(result.returncode,expected,result.stdout+'\n'+result.stderr)
        self.assert_no_sensitive_output(result)
        return result

    def diagnostics(self, result):
        return set(re.findall(r'DIAGNOSTICO\s+(\S+)',result.stdout))

    def recorded_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_no_sensitive_output(self, result):
        output=result.stdout+result.stderr+(self.report.read_text() if self.report.exists() else '')
        recorded=self.calls.read_text() if self.calls.exists() else ''
        for key in ('old_value','new_value','token'):
            self.assertNotIn(self.fixture[key],output)
            self.assertNotIn(self.fixture[key],recorded)
        for call in self.recorded_calls():
            args=call['args']
            if call['tool']=='oc':
                self.assertTrue('get' in args or ('config' in args and 'view' in args),args)
                self.assertNotIn('/should/not/use/ambient-config',args)
            else:
                self.assertFalse(any('/admin/' in item for item in args),args)
                self.assertNotIn('-k',args)
                self.assertNotIn('--insecure',args)
                self.assertNotIn('--location',args)

    def test_once_observes_both_secrets_without_rotating_or_leaking(self):
        result=self.run_watch('--once',expected=0)
        self.assertIn('v0001',result.stdout)
        self.assertIn('v0002',result.stdout)
        self.assertIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
        self.assertEqual(len([x for x in self.recorded_calls() if x['tool']=='curl']),2)
        self.assertEqual(list(self.temp.iterdir()),[])

    def test_pending_rotation_with_stale_metadata_waits_for_secret(self):
        self.fixture.update(state='pending',missing_new=True)
        result=self.run_watch('--once',expected=0)
        self.assertIn('AGUARDANDO',result.stdout)
        self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))
        self.assertNotIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))

    def test_full_automation_cycle_is_observed_in_a_bounded_session(self):
        self.fixture['state']='cycling'
        result=self.run_watch('--duration','14',expected=0)
        self.assertIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
        self.assertIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))
        self.assertIn('401',result.stdout)
        self.assertEqual(list(self.temp.iterdir()),[])

    def test_starting_after_overlap_cannot_prove_unobserved_coexistence(self):
        self.fixture['state']='expired'
        result=self.run_watch('--once',expected=0)
        self.assertIn('INCONCLUSIVO',result.stdout)
        self.assertNotIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
        self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))

    def test_network_failure_or_forbidden_is_not_expiration(self):
        original=self.fixture.copy()
        for change in ({'old_errno':60},{'old_http':403,'old_error':'access_denied'}):
            with self.subTest(change=change):
                self.fixture={**original,**change}
                result=self.run_watch('--once',expected=0)
                self.assertEqual(len([call for call in self.recorded_calls() if call['tool']=='curl']),2)
                self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))
                self.assertNotIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
                self.fixture.pop('old_errno',None);self.fixture.pop('old_http',None);self.fixture.pop('old_error',None)

    def test_failures_after_observed_overlap_do_not_prove_expiration(self):
        original=self.fixture.copy()
        for change in ({'post_expiry_errno':60},{'post_expiry_http':403}):
            with self.subTest(change=change):
                self.fixture={**original,'state':'cycling',**change}
                result=self.run_watch('--duration','14',expected=0)
                self.assertIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
                self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))
                expected='ERRO_REDE_OU_TLS' if 'post_expiry_errno' in change else '403'
                self.assertIn(expected,result.stdout)

    def test_mid_round_cycle_change_discards_comparison(self):
        self.fixture['race']=True
        result=self.run_watch('--once')
        self.assertNotIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
        self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))
        self.assertIn('RODADA_DESCARTADA_ESTADO_MUDOU',result.stdout)

    def test_wrong_client_and_mutable_secret_are_refused_before_authentication(self):
        for change in ({'client_id':'another-client'},{'mutable':True}):
            with self.subTest(change=change):
                self.fixture.update(change)
                self.calls.unlink(missing_ok=True)
                result=self.run_watch('--once')
                self.assertNotEqual(result.returncode,0)
                self.assertIn('Secret não é imutável ou diverge',result.stderr)
                self.assertFalse(any(call['tool']=='curl' for call in self.recorded_calls()))
                self.fixture.pop('client_id',None);self.fixture.pop('mutable',None)

    def test_wrong_cluster_or_insecure_kubeconfig_is_refused(self):
        for change in ({'server':'https://different.example.test:6443'},{'insecure':True}):
            with self.subTest(change=change):
                self.fixture.update(change)
                self.calls.unlink(missing_ok=True)
                result=self.run_watch('--once')
                self.assertNotEqual(result.returncode,0)
                self.assertIn('servidor/contexto inesperado ou TLS Kubernetes desabilitado',result.stderr)
                self.assertFalse(any(call['tool']=='curl' for call in self.recorded_calls()))
                self.fixture.pop('server',None);self.fixture.pop('insecure',None)

    def test_http_200_without_access_token_is_not_authentication(self):
        self.fixture['missing_token']=True
        result=self.run_watch('--once',expected=0)
        self.assertEqual(len([call for call in self.recorded_calls() if call['tool']=='curl']),2)
        self.assertNotIn('CONVIVENCIA_OBSERVADA',self.diagnostics(result))
        self.assertNotIn('PASS_EXPIRACAO_OBSERVADA',self.diagnostics(result))


if __name__=='__main__':
    unittest.main()
