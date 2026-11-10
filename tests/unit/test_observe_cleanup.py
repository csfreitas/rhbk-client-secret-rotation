import secrets
TEST_VALUE = secrets.token_urlsafe(18)
"""Observation output and destructive-scope guards; no cluster calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import lab

def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/(name+'.py'))
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
observe=load('observe');cleanup=load('cleanup')

class ObserveCleanupSafety(unittest.TestCase):
 def test_cleanup_requires_recorded_identity_before_live_guard(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(lab,'ROOT',Path(directory)),patch.object(lab,'guard') as guard:
   with self.assertRaisesRegex(RuntimeError,'recorded cluster identity'):
    cleanup.targets()
   guard.assert_not_called()

 def test_cleanup_refuses_unowned_namespace(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'evidence').mkdir();(root/'evidence/cluster-identity.json').write_text('{}')
   def get(kind,name,ns):return {'kind':'Namespace','metadata':{'name':lab.NS,'uid':'unit-uid','labels':{}}} if kind=='namespace' else None
   with patch.object(lab,'ROOT',root),patch.object(lab,'guard'),patch.object(lab,'get',get),self.assertRaisesRegex(RuntimeError,'ownership'):
    cleanup.targets()

 def test_cleanup_raw_delete_uses_uid_precondition(self):
  namespace={'kind':'Namespace','metadata':{'name':lab.NS,'uid':'unit-uid','labels':lab.LABELS}}
  with patch.object(sys,'argv',['cleanup.py']),patch.object(cleanup,'targets',return_value=[('/api/v1/namespaces/'+lab.NS,namespace)]),patch.object(lab,'guard'),patch.object(lab,'get',return_value=None),patch.object(lab,'oc') as oc,patch.object(lab,'save'),contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(cleanup.main(),0)
   self.assertEqual(oc.call_args.kwargs['data']['preconditions'],{'uid':'unit-uid'})
   self.assertEqual(oc.call_args.args,('delete','--raw=/api/v1/namespaces/'+lab.NS,'-f','-'))

 def test_cleanup_dry_run_does_not_delete(self):
  namespace={'kind':'Namespace','metadata':{'name':lab.NS,'uid':'unit-uid','labels':lab.LABELS}}
  with patch.object(sys,'argv',['cleanup.py','--dry-run']),patch.object(cleanup,'targets',return_value=[('/api/v1/namespaces/'+lab.NS,namespace)]),patch.object(lab,'oc') as oc,patch.object(lab,'save'),contextlib.redirect_stdout(io.StringIO()):
   self.assertEqual(cleanup.main(),0);oc.assert_not_called()

 def test_observer_discards_sensitive_log_fields(self):
  pod={'metadata':{'name':'unit-pod','labels':{'app.kubernetes.io/name':'m2m-probe'}}}
  raw=json.dumps({'event':'token_attempt','http_status':200,'access_token':TEST_VALUE,'client_secret':TEST_VALUE})
  with patch.object(lab,'oc',return_value=raw):
   self.assertEqual(observe.logs(pod),[{'event':'token_attempt','http_status':200}])

 def test_observer_state_uses_numeric_metadata_allowlist(self):
  obj={'metadata':{'labels':lab.LABELS},'data':{'state.json':json.dumps({'phase':'IDLE','client_secret':TEST_VALUE,'post_metadata':{'client.secret.expiration.time':1234,'client.secret.rotated':TEST_VALUE}})}}
  with patch.object(lab,'get',return_value=obj):
   self.assertEqual(observe.safe_state(),{'phase':'IDLE','post_metadata':{'client.secret.expiration.time':1234}})

 def test_table_does_not_mix_old_probe_failures_with_main(self):
  main={'name':'app-pod','generation':'v0016','status_available':True,
        'last_http':200,'successes':54,'failures':0,'latest_authentication_ok':True,
        'last_attempt_utc':'2026-11-10T16:13:00Z'}
  data={'state':{'phase':'COMPLETE','generation':16,'old_generation':15,'suspended':False},
        'cronjob':{'suspended':False},'main_application':[main],
        'old_secret_probes':[{'generation':'v0015','status_available':True,'last_http':401,'failures':999}]}
  now=observe.dt.datetime.fromisoformat('2026-11-10T16:13:02+00:00').timestamp()
  result=observe.table(data,now=now)
  self.assertIn('13:13:02',result)
  self.assertIn('ATIVA',result)
  self.assertIn('HTTP 401 (separada da aplicacao)',result)
  self.assertNotIn('999',result)
  self.assertEqual(observe.authentication_status(main,now),'OK')

 def test_missing_or_stale_status_is_never_shown_as_healthy(self):
  now=observe.dt.datetime.fromisoformat('2026-11-10T16:13:30+00:00').timestamp()
  self.assertEqual(observe.authentication_status({'ready':True},now),'SEM STATUS')
  self.assertEqual(observe.authentication_status({'status_available':True,'last_http':200},now),'SEM AMOSTRA')
  stale={'status_available':True,'last_http':200,'latest_authentication_ok':True,
         'last_attempt_utc':'2026-11-10T16:13:00Z'}
  self.assertEqual(observe.authentication_status(stale,now),'DESATUALIZADA')

 def test_watch_saves_json_while_printing_a_table(self):
  row={'timestamp_utc':'2026-11-10T16:13:00Z','state':{},'deployment':{},'cronjob':{},
       'main_application':[],'old_secret_probes':[]}
  with tempfile.TemporaryDirectory() as directory:
   destination=Path(directory)/'watch.jsonl'
   with patch.object(observe,'snapshot',return_value=row),patch.object(observe.time,'sleep',side_effect=KeyboardInterrupt),contextlib.redirect_stdout(io.StringIO()) as output,contextlib.redirect_stderr(io.StringIO()):
    self.assertEqual(observe.watch(output=destination),0)
   self.assertEqual(json.loads(destination.read_text()),row)
   self.assertIn('Nenhum pod',output.getvalue())

if __name__=='__main__':unittest.main()
