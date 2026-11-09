"""Bootstrap fallback boundaries for T15. All transports are local fakes."""
import secrets
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('mapper_setup',ROOT/'tests/integration/mapper_setup.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
CID='12345678-1234-1234-1234-123456789abc'
NAME='expiry-token-vs-secret-123456789a'
BOOT_TOKEN=secrets.token_urlsafe(24)
RUNTIME_TOKEN=secrets.token_urlsafe(24)
TEST_PASSWORD=secrets.token_urlsafe(24)


class Transport:
 def __init__(self):
  self.context=ssl.create_default_context();self.calls=[];self.mapper=None
  self.realm_owner=helper.OWNER;self.client_owner=helper.OWNER;self.response_code=201;self.persist=True
 def request(self,method,url,body=None,token=None,form=False):
  self.calls.append({'method':method,'url':url,'body':body,'token':token})
  if url.endswith('/realms/master/protocol/openid-connect/token'):
   return 200,{'access_token':BOOT_TOKEN},{}
  if url.endswith('/admin/realms/'+helper.REALM):
   return 200,{'realm':helper.REALM,'attributes':{'lab.rhbk/owner':self.realm_owner}},{}
  if url.endswith('/clients/'+CID):
   return 200,{'id':CID,'clientId':NAME,'protocol':'openid-connect','publicClient':False,'attributes':{'lab.owner':self.client_owner,'lab.experiment':'token-vs-secret'}},{}
  if url.endswith('/protocol-mappers/models'):
   if method=='GET':return 200,[] if self.mapper is None else [self.mapper],{}
   if method=='POST':
    if self.persist:self.mapper=copy.deepcopy(body)
    return self.response_code,{},{}
  raise AssertionError('unexpected endpoint')


class API:
 def __init__(self):
  self.url=helper.URL;self.realm=helper.REALM;self.admin_id='rotation-admin';self.token=RUNTIME_TOKEN;self.http=Transport()


class MapperSetupTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  (self.root/'evidence').mkdir();(self.root/'evidence/cluster-identity.json').write_text('{"uid":"cluster-test"}')
  (self.root/'evidence/namespace-identity.json').write_text(json.dumps({'name':helper.NS,'uid':'namespace-test','cluster_uid':'cluster-test'}))
  self.root_patch=patch.object(helper,'ROOT',self.root);self.root_patch.start()
  self.env_patch=patch.dict(os.environ,{'KUBECONFIG':str(self.root/'.private/crc.kubeconfig')});self.env_patch.start()
  self.api=API();self.oc_calls=[];self.namespace_owned=True;self.cluster_uid='cluster-test'
 def tearDown(self):self.env_patch.stop();self.root_patch.stop();self.temp.cleanup()
 def oc(self,args):
  self.oc_calls.append(args)
  if args[0]=='config':return json.dumps({'clusters':[{'cluster':{'server':'https://api.crc.testing:6443'}}]})
  if 'kube-system' in args:return json.dumps({'metadata':{'uid':self.cluster_uid}})
  if 'route' in args:return json.dumps({'metadata':{'labels':helper.LABELS},'spec':{'host':'rhbk-rotation.apps-crc.testing'}})
  if 'secret' in args:return json.dumps({'metadata':{'labels':helper.LABELS},'data':{'username':base64.b64encode(b'unit-user').decode(),'password':base64.b64encode(TEST_PASSWORD.encode()).decode()}})
  return json.dumps({'metadata':{'uid':'namespace-test','labels':helper.LABELS if self.namespace_owned else {}}})
 def run_setup(self,**kwargs):return helper.configure(self.api,CID,NAME,self.oc,trigger_status=kwargs.get('trigger_status',403))
 def mapper_posts(self):return [c for c in self.api.http.calls if c['method']=='POST' and c['url'].endswith('/protocol-mappers/models')]

 def test_only_exact_403_allows_bootstrap_fallback(self):
  for code in (0,401,500):
   with self.assertRaisesRegex(helper.SetupBlocked,'requires_runtime_POST_403'):self.run_setup(trigger_status=code)
  self.assertEqual(self.oc_calls,[]);self.assertEqual(self.api.http.calls,[])

 def test_main_client_refused_before_credentials_are_read(self):
  with self.assertRaisesRegex(helper.SetupBlocked,'not_disposable'):
   helper.configure(self.api,CID,'catalogo-api',self.oc,trigger_status=403)
  self.assertEqual(self.oc_calls,[])

 def test_tls_must_remain_validated(self):
  self.api.http.context.check_hostname=False
  with self.assertRaisesRegex(helper.SetupBlocked,'validated_TLS'):self.run_setup()
  self.assertEqual(self.oc_calls,[])

 def test_cluster_uid_mismatch_refuses_secret_read(self):
  self.cluster_uid='other-cluster'
  with self.assertRaisesRegex(helper.SetupBlocked,'identity_changed'):self.run_setup()
  self.assertFalse(any('secret' in args for args in self.oc_calls));self.assertEqual(self.api.http.calls,[])

 def test_foreign_namespace_refused_before_bootstrap_authentication(self):
  self.namespace_owned=False
  with self.assertRaisesRegex(helper.SetupBlocked,'ownership_mismatch'):self.run_setup()
  self.assertEqual(self.api.http.calls,[])

 def test_another_checkout_namespace_refused_before_secret_read(self):
  (self.root/'evidence/namespace-identity.json').write_text(json.dumps({'name':helper.NS,'uid':'another-namespace','cluster_uid':'cluster-test'}))
  with self.assertRaisesRegex(helper.SetupBlocked,'namespace_not_created'):self.run_setup()
  self.assertFalse(any('secret' in args for args in self.oc_calls));self.assertEqual(self.api.http.calls,[])

 def test_foreign_realm_refuses_mapper_mutation(self):
  self.api.http.realm_owner='another-project'
  with self.assertRaisesRegex(helper.SetupBlocked,'realm_not_owned'):self.run_setup()
  self.assertEqual(self.mapper_posts(),[])

 def test_foreign_client_refuses_mapper_mutation(self):
  self.api.http.client_owner='another-project'
  with self.assertRaisesRegex(helper.SetupBlocked,'client_not_owned'):self.run_setup()
  self.assertEqual(self.mapper_posts(),[])

 def test_setup_changes_only_one_mapper_and_preserves_runtime_identity(self):
  receipt=self.run_setup()
  self.assertEqual(len(self.mapper_posts()),1)
  self.assertEqual(self.mapper_posts()[0]['body'],helper.model(NAME))
  self.assertEqual(self.api.token,RUNTIME_TOKEN)
  self.assertEqual(self.api.admin_id,'rotation-admin')
  self.assertFalse(receipt['runtime_permissions_changed']);self.assertFalse(receipt['client_secret_mutation_sent'])
  serialized=json.dumps(receipt)
  for value in (TEST_PASSWORD,RUNTIME_TOKEN,BOOT_TOKEN):self.assertNotIn(value,serialized)
  self.assertEqual(sum(c['method']=='POST' for c in self.api.http.calls),2)

 def test_existing_correct_mapper_is_read_without_post(self):
  self.api.http.mapper=helper.model(NAME)
  receipt=self.run_setup();self.assertEqual(receipt['action'],'existing_mapper_verified');self.assertEqual(self.mapper_posts(),[])

 def test_existing_conflicting_mapper_is_not_overwritten(self):
  self.api.http.mapper=helper.model(NAME);self.api.http.mapper['config']['included.client.audience']='foreign'
  with self.assertRaisesRegex(helper.SetupBlocked,'mapper_conflicts'):self.run_setup()
  self.assertEqual(self.mapper_posts(),[])

 def test_known_server_boolean_string_defaults_are_accepted(self):
  expected=helper.model(NAME)
  for introspection in ('true','false'):
   for userinfo in ('true','false'):
    with self.subTest(introspection=introspection,userinfo=userinfo):
     actual=copy.deepcopy(expected)
     actual['config'].update({'introspection.token.claim':introspection,'userinfo.token.claim':userinfo})
     self.assertTrue(helper.exact_mapper(actual,expected))

 def test_changed_audience_unknown_extra_or_nonboolean_default_is_refused(self):
  expected=helper.model(NAME)
  changes=[{'included.client.audience':'another-client'},
           {'unexpected.token.claim':'true'},
           {'introspection.token.claim':'yes'},
           {'userinfo.token.claim':True}]
  for change in changes:
   with self.subTest(change=change):
    actual=copy.deepcopy(expected);actual['config'].update(change)
    self.assertFalse(helper.exact_mapper(actual,expected))

 def test_lost_response_is_reconciled_by_get_without_second_post(self):
  self.api.http.response_code=0
  receipt=self.run_setup();self.assertEqual(receipt['action'],'mapper_outcome_reconciled_by_GET');self.assertEqual(len(self.mapper_posts()),1)

 def test_unknown_outcome_without_readback_blocks_instead_of_retrying(self):
  self.api.http.response_code=0;self.api.http.persist=False
  with self.assertRaisesRegex(helper.SetupBlocked,'no_retry'):self.run_setup()
  self.assertEqual(len(self.mapper_posts()),1)

if __name__=='__main__':unittest.main()
