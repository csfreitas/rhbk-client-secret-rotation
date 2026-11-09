"""Proxy requests must not forward an oc timeout query to the probe HTTP path.

The subprocess still has a finite wall-clock bound. All oc calls are mocked;
these tests never access a cluster or execute a shell command.
"""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]


def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

lab=load('lab_proxy_timeout_test',ROOT/'scripts/lab.py')
operations=load('operations_proxy_timeout_test',ROOT/'tests/integration/operations.py')
PROXY='/api/v1/namespaces/rhbk-rotation-lab/pods/m2m-probe-test:8080/proxy/status'


class ProxyTimeoutTests(unittest.TestCase):
 def setUp(self):
  directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
  root=Path(directory.name);private=root/'.private';private.mkdir()
  (private/'crc.kubeconfig').write_text('test fixture; no credentials')
  for target,name,value in ((operations,'ROOT',root),(lab,'PRIVATE',private)):
   patched=patch.object(target,name,value);patched.start();self.addCleanup(patched.stop)

 def success(self):return subprocess.CompletedProcess(['oc'],0,stdout=b'{}',stderr=b'')

 def test_lab_proxy_disables_forwarded_query_but_keeps_35_second_process_bound(self):
  with patch.object(lab.subprocess,'run',return_value=self.success()) as run:
   self.assertEqual(lab.oc('get','--raw',PROXY),'{}')
  argv=run.call_args.args[0]
  self.assertIn('--request-timeout=0',argv)
  self.assertEqual(run.call_args.kwargs['timeout'],35)
  self.assertEqual(argv[-3:],['get','--raw',PROXY])
  self.assertNotIn('shell',run.call_args.kwargs)

 def test_operations_proxy_disables_forwarded_query_but_keeps_30_second_process_bound(self):
  crc=operations.CRC.__new__(operations.CRC)
  with patch.object(operations.subprocess,'run',return_value=self.success()) as run:
   self.assertEqual(crc.oc('get','--raw',PROXY),(0,'{}'))
  self.assertIn('--request-timeout=0',run.call_args.args[0])
  self.assertEqual(run.call_args.kwargs['timeout'],30)

 def test_lab_normal_and_nonproxy_raw_requests_keep_api_timeout(self):
  cases=[('get','pods','-n','rhbk-rotation-lab'),('get','--raw','/api/v1/namespaces/rhbk-rotation-lab/pods'),('get','configmap','a/proxy/name')]
  for args in cases:
   with self.subTest(args=args),patch.object(lab.subprocess,'run',return_value=self.success()) as run:
    lab.oc(*args)
    self.assertIn('--request-timeout=30s',run.call_args.args[0])
    self.assertNotIn('--request-timeout=0',run.call_args.args[0])
    self.assertEqual(run.call_args.kwargs['timeout'],35)

 def test_operations_normal_and_nonproxy_raw_requests_keep_api_timeout(self):
  crc=operations.CRC.__new__(operations.CRC)
  cases=[('get','pods','-n','rhbk-rotation-lab'),('get','--raw','/api/v1/namespaces/rhbk-rotation-lab/pods'),('get','configmap','a/proxy/name')]
  for args in cases:
   with self.subTest(args=args),patch.object(operations.subprocess,'run',return_value=self.success()) as run:
    crc.oc(*args)
    self.assertIn('--request-timeout=20s',run.call_args.args[0])
    self.assertNotIn('--request-timeout=0',run.call_args.args[0])
    self.assertEqual(run.call_args.kwargs['timeout'],30)

 def test_proxy_process_timeout_is_reported_without_unbounded_retry(self):
  crc=operations.CRC.__new__(operations.CRC)
  with patch.object(operations.subprocess,'run',side_effect=subprocess.TimeoutExpired(['oc'],30)) as run:
   with self.assertRaisesRegex(operations.Blocked,'oc_unavailable_or_timed_out'):
    crc.oc('get','--raw',PROXY)
  self.assertEqual(run.call_count,1)

if __name__=='__main__':unittest.main()
