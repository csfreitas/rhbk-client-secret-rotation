"""Image provenance and build boundaries. No subprocess or cluster is used."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
spec = importlib.util.spec_from_file_location('rhbk_image', ROOT / 'scripts/rhbk_image.py')
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)


class ImageBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for filename in ('versions.lock.yaml', 'apps/rhbk/Dockerfile', 'deploy/build/rhbk-buildconfig.json'):
            target = self.root / filename; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
        (self.root / 'evidence').mkdir()
        (self.root / 'evidence/namespace-identity.json').write_text(json.dumps({'uid':'ns-uid','cluster_uid':'cluster-uid'}))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(lab, 'ROOT', self.root))
        self.stack.enter_context(patch.object(lab, 'PRIVATE', self.root / '.private'))
        self.guard = self.stack.enter_context(patch.object(lab, 'guard'))
        self.oc = self.stack.enter_context(patch.object(lab, 'oc'))
        self.apply = self.stack.enter_context(patch.object(lab, 'apply'))
        self.get = self.stack.enter_context(patch.object(lab, 'get', side_effect=self.read))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.pin = image.inputs()
        self.config = image.render()['items'][1]
        self.config['metadata']['uid'] = 'config-uid'
        self.output_digest = 'sha256:' + 'a' * 64
        self.build = {'metadata': {'name':'rhbk-optimized-1','namespace':lab.NS,'uid':'build-uid',
                      'labels':dict(lab.LABELS),'ownerReferences':[{'kind':'BuildConfig','name':image.NAME,'uid':'config-uid'}]},
                      'spec':copy.deepcopy(self.config['spec']),
                      'status':{'phase':'Complete','output':{'to':{'imageDigest':self.output_digest}}}}
        self.objects = {('imagestream','rhbk'):{'metadata':lab.meta('rhbk')},
                        ('imagestreamimage','rhbk@'+self.pin['base_digest']):{
                            'metadata':{'name':'rhbk@'+self.pin['base_digest'],'namespace':lab.NS},
                            'image':{'metadata':{'name':self.pin['base_digest']}}},
                        ('buildconfig',image.NAME):self.config,
                        ('build','rhbk-optimized-1'):self.build,
                        ('imagestreamtag','rhbk-optimized:lab'):{'image':{'metadata':{'name':self.output_digest}}},
                        ('builds',None):{'items':[]}}

    def read(self, kind, name=None, *args):
        return copy.deepcopy(self.objects.get((kind,name)))

    def test_dockerfile_uses_two_pinned_stages_and_optimized_command(self):
        text = (self.root / 'apps/rhbk/Dockerfile').read_text()
        self.assertEqual(text.count('FROM ${RHBK_IMAGE}'),2)
        self.assertIn('ARG RHBK_IMAGE\n',text)
        self.assertNotIn('ARG RHBK_IMAGE=',text)
        self.assertIn('test "${#digest}" -eq 64',text)
        self.assertIn('*[!0-9a-f]*',text)
        for option in ('--features=client-secret-rotation','--db=postgres','--health-enabled=true','--metrics-enabled=true'):
            self.assertIn(option,text)
        self.assertIn('CMD ["start", "--optimized"]',text)
        self.assertNotIn('PASSWORD=',text)

    def test_render_is_local_and_has_no_mutating_trigger(self):
        result = image.render()
        self.oc.assert_not_called(); self.get.assert_not_called(); self.guard.assert_not_called()
        self.assertEqual([x['kind'] for x in result['items']],['ImageStream','BuildConfig'])
        spec = result['items'][1]['spec']
        self.assertEqual(spec['triggers'],[])
        self.assertEqual(spec['strategy']['dockerStrategy']['buildArgs'],[{'name':'RHBK_IMAGE','value':self.pin['base_image']}])
        self.assertEqual(spec['nodeSelector'],{'kubernetes.io/arch':'arm64'})

    def test_api_may_omit_empty_triggers_but_must_not_add_any(self):
        del self.config['spec']['triggers']
        self.oc.return_value = 'build.build.openshift.io/rhbk-optimized-1\n'
        result = image.build()
        self.assertEqual(result['status'],'BUILD_COMPLETE')
        self.config['spec']['triggers'] = [{'type':'ConfigChange'}]
        with self.assertRaisesRegex(RuntimeError,'BuildConfig no longer matches'):
            image.observe_build('rhbk-optimized-1')
        with self.assertRaisesRegex(RuntimeError,'BuildConfig readback differs'):
            image.build()

    def test_tag_or_nonofficial_base_is_rejected(self):
        path = self.root / 'versions.lock.yaml'; original = json.loads(path.read_text())
        for bad in ('registry.redhat.io/rhbk/keycloak-rhel9:latest','example.invalid/keycloak@sha256:'+'f'*64):
            changed = copy.deepcopy(original); changed['images']['rhbk']['original'] = bad
            path.write_text(json.dumps(changed))
            with self.subTest(base=bad), self.assertRaisesRegex(RuntimeError,'official repository'): image.inputs()

    def test_missing_reference_has_no_remote_fallback(self):
        with self.assertRaisesRegex(RuntimeError,'build the optimized'): image.load_reference()
        self.oc.assert_not_called(); self.get.assert_not_called()

    def test_wrong_mirror_refuses_before_any_mutation(self):
        self.objects[('imagestreamimage','rhbk@'+self.pin['base_digest'])]['image']['metadata']['name'] = 'sha256:'+'b'*64
        with self.assertRaisesRegex(RuntimeError,'mirror the pinned'): image.build()
        self.guard.assert_called_once(); self.apply.assert_not_called(); self.oc.assert_not_called()

    def test_base_stream_identity_is_required(self):
        self.objects[('imagestream','rhbk')]['metadata']['namespace'] = 'other'
        with self.assertRaisesRegex(RuntimeError,'identity differs'): image.build()
        self.apply.assert_not_called()

    def test_read_only_legacy_mirror_needs_no_ownership_labels_or_mutation(self):
        self.objects[('imagestream','rhbk')]['metadata']['labels'] = {}
        image.verify_mirror(self.pin)
        self.oc.assert_not_called(); self.apply.assert_not_called()
        self.assertNotIn(('imagestreamtag','rhbk:pinned'),self.objects)
        self.get.assert_any_call('imagestreamimage','rhbk@'+self.pin['base_digest'])

    def test_mirror_image_wrong_namespace_or_identity_refused(self):
        target = self.objects[('imagestreamimage','rhbk@'+self.pin['base_digest'])]
        for key in ('name','namespace'):
            original = target['metadata'][key]
            target['metadata'][key] = 'other'
            with self.subTest(field=key), self.assertRaisesRegex(RuntimeError,'ImageStreamImage identity'):
                image.verify_mirror(self.pin)
            target['metadata'][key] = original
        self.oc.assert_not_called(); self.apply.assert_not_called()

    def test_base_override_or_inline_source_refused_before_start(self):
        for key, value in [('dockerfile','FROM example.invalid/other'), ('secrets',[{'secret':{'name':'private'}}])]:
            self.config['spec']['source'][key] = value
            with self.subTest(field=key), self.assertRaisesRegex(RuntimeError,'unexpected Build source'): image.build()
            self.oc.assert_not_called()
            del self.config['spec']['source'][key]
        self.config['spec']['strategy']['dockerStrategy']['from'] = {'kind':'DockerImage','name':'example.invalid/other'}
        with self.assertRaisesRegex(RuntimeError,'overrides the pinned'): image.build()
        self.oc.assert_not_called()

    def test_upload_contains_only_snapshotted_dockerfile_and_never_deploys(self):
        (self.root / 'apps/rhbk/never-upload.secret').write_text('fixture')
        def start(*args, **kwargs):
            self.assertEqual(args[0],'start-build')
            directory = Path(next(x.split('=',1)[1] for x in args if x.startswith('--from-dir=')))
            self.assertEqual([x.name for x in directory.iterdir()],['Dockerfile'])
            self.assertEqual((directory/'Dockerfile').read_bytes(),self.pin['source'])
            return 'build.build.openshift.io/rhbk-optimized-1\n'
        self.oc.side_effect = start
        result = image.build()
        self.assertEqual(result['status'],'BUILD_COMPLETE')
        self.assertEqual(self.oc.call_count,1)
        self.assertEqual(self.apply.call_args.args[0]['items'][1]['kind'],'BuildConfig')
        self.assertEqual(image.load_reference(),result['image'])
        self.assertEqual(result['runtime_validation'],'NOT_RUN')
        self.assertFalse((lab.PRIVATE / image.PENDING).exists())

    def test_changed_output_digest_never_records_success(self):
        self.objects[('imagestreamtag','rhbk-optimized:lab')]['image']['metadata']['name'] = 'sha256:'+'c'*64
        with self.assertRaisesRegex(RuntimeError,'output tag differs'): image.observe_build('rhbk-optimized-1')
        self.assertFalse((lab.PRIVATE / image.RECORD).exists())

    def test_wrong_owner_or_source_rejected(self):
        for field in ('owner','source'):
            changed = copy.deepcopy(self.build)
            if field == 'owner': changed['metadata']['ownerReferences'][0]['uid'] = 'foreign'
            else: changed['spec']['output']['imageLabels'][0]['value'] = 'f'*64
            self.objects[('build','rhbk-optimized-1')] = changed
            with self.subTest(field=field), self.assertRaises(RuntimeError): image.observe_build('rhbk-optimized-1')
            self.assertFalse((lab.PRIVATE / image.RECORD).exists())

    def test_reference_refuses_source_or_namespace_drift(self):
        image.observe_build('rhbk-optimized-1')
        dockerfile = self.root / 'apps/rhbk/Dockerfile'; original = dockerfile.read_text()
        dockerfile.write_text(original+'\n# change\n')
        with self.assertRaisesRegex(RuntimeError,'stale'): image.load_reference()
        dockerfile.write_text(original)
        (self.root/'evidence/namespace-identity.json').write_text(json.dumps({'uid':'other','cluster_uid':'cluster-uid'}))
        with self.assertRaisesRegex(RuntimeError,'another cluster'): image.load_reference()

    def test_lost_start_response_keeps_intent_and_refuses_automatic_retry(self):
        self.oc.side_effect = RuntimeError('transport fixture')
        with self.assertRaises(RuntimeError): image.build()
        self.assertEqual(json.loads((lab.PRIVATE / image.PENDING).read_text())['phase'],'REQUESTED')
        count = self.oc.call_count
        with self.assertRaisesRegex(RuntimeError,'pending'): image.build()
        self.assertEqual(self.oc.call_count,count)

    def test_timeout_is_bounded_and_does_not_cancel_or_rebuild(self):
        self.objects[('build','rhbk-optimized-1')]['status']['phase'] = 'Running'
        with patch.object(image.time,'monotonic',side_effect=[0,0,2]), patch.object(image.time,'sleep'), \
             self.assertRaisesRegex(RuntimeError,'timed out'): image.observe_build('rhbk-optimized-1',1)
        self.oc.assert_not_called(); self.apply.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'between 1 and 1800'): image.build(0)

    def test_confirmed_failed_build_can_be_retried_without_pin(self):
        self.objects[('build','rhbk-optimized-1')]['status']['phase'] = 'Failed'
        image.private_record(image.PENDING,{'build':'rhbk-optimized-1'})
        with self.assertRaisesRegex(RuntimeError,'did not complete'): image.observe_build('rhbk-optimized-1')
        self.assertFalse((lab.PRIVATE / image.RECORD).exists())
        self.assertFalse((lab.PRIVATE / image.PENDING).exists())


if __name__ == '__main__': unittest.main()
