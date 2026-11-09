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

if __name__=='__main__':unittest.main()
