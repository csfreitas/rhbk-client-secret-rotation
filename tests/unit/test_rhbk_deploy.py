"""Existing single-replica RHBK must not restart in the middle of a rotation."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import lab


class DeploymentMaintenance(unittest.TestCase):
    def resources(self, suspended=True, phase='COMPLETE', jobs=None):
        return {
            ('keycloak', 'rhbk'): {'metadata': lab.meta('rhbk')},
            ('cronjob', 'rotation-controller'): {
                'metadata': lab.meta('rotation-controller'), 'spec': {'suspend': suspended}},
            ('configmap', 'rotation-state'): {
                'metadata': lab.meta('rotation-state'),
                'data': {'state.json': json.dumps({'suspended': suspended, 'phase': phase})}},
            ('jobs', None): {'items': jobs or []}}

    def test_active_automation_blocks_before_any_deployment_write(self):
        resources = self.resources(suspended=False)
        with patch.object(lab, 'guard'), patch.object(lab, 'get', side_effect=lambda k, n=None: resources.get((k, n))), \
             patch.object(lab, 'create_secret') as create, patch.object(lab, 'apply') as apply:
            with self.assertRaisesRegex(RuntimeError, 'suspend rotation'):
                lab.deploy()
            create.assert_not_called(); apply.assert_not_called()

    def test_incomplete_cycle_is_not_a_maintenance_window(self):
        resources = self.resources(phase='POST_IN_FLIGHT')
        with patch.object(lab, 'get', side_effect=lambda k, n=None: resources.get((k, n))):
            with self.assertRaisesRegex(RuntimeError, 'finish its current cycle'):
                lab.require_maintenance_window()

    def test_pending_job_blocks_even_without_active_count(self):
        resources = self.resources(jobs=[{'status': {}}])
        with patch.object(lab, 'get', side_effect=lambda k, n=None: resources.get((k, n))):
            with self.assertRaisesRegex(RuntimeError, 'Jobs to finish'):
                lab.require_maintenance_window()

    def test_completed_job_and_suspended_terminal_state_allow_maintenance(self):
        resources = self.resources(jobs=[{'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]}}])
        with patch.object(lab, 'get', side_effect=lambda k, n=None: resources.get((k, n))):
            lab.require_maintenance_window()

    def test_fresh_installation_has_no_rotation_to_pause(self):
        with patch.object(lab, 'get', return_value=None) as get:
            lab.require_maintenance_window()
            get.assert_called_once_with('keycloak', 'rhbk')

    def test_committed_cr_requires_built_image_and_optimized_start(self):
        resource = json.loads((lab.ROOT / 'deploy/rhbk/resources.json').read_text())['items'][0]
        self.assertEqual(resource['spec']['image'], 'rhbk-optimized:build-required')
        self.assertIs(resource['spec']['startOptimized'], True)
        self.assertNotIn('features', resource['spec'])


class DatabasePreservation(unittest.TestCase):
    def resources(self):
        items = json.loads((lab.ROOT/'deploy/database/resources.json').read_text())['items']
        for obj in items:
            obj['metadata'].update(uid='uid-'+obj['kind'],generation=1)
        items[0]['status'] = {'phase':'Bound'}
        items[2]['status'] = {'observedGeneration':1,'availableReplicas':1,'updatedReplicas':1}
        return items

    def test_owned_ready_database_is_preserved_for_origin_or_mirror(self):
        for image in (self.resources()[2]['spec']['template']['spec']['containers'][0]['image'],lab.PG_IMAGE):
            resources = self.resources(); resources[2]['spec']['template']['spec']['containers'][0]['image'] = image
            credential = {'metadata':lab.meta('postgres-credential'),'data':{'username':'fixture','password':'fixture'}}
            with self.subTest(image=image), patch.object(lab,'database_resources',return_value=resources), \
                 patch.object(lab,'get',return_value=credential), patch.object(lab,'save'), \
                 patch.object(lab,'oc') as oc, patch.object(lab,'apply') as apply, patch.object(lab,'create_secret') as create:
                self.assertEqual(lab.prepare_database()['action'],'PRESERVED')
                oc.assert_not_called(); apply.assert_not_called(); create.assert_not_called()

    def test_partial_unowned_wrong_digest_or_unready_database_blocks_without_write(self):
        cases = []
        items = self.resources(); items[1] = None; cases.append(items)
        items = self.resources(); items[0]['metadata']['labels'] = {}; cases.append(items)
        items = self.resources(); items[2]['spec']['template']['spec']['containers'][0]['image'] = 'postgres:latest'; cases.append(items)
        items = self.resources(); items[2]['status']['updatedReplicas'] = 0; cases.append(items)
        for resources in cases:
            with self.subTest(resources=cases.index(resources)), patch.object(lab,'database_resources',return_value=resources), \
                 patch.object(lab,'oc') as oc, patch.object(lab,'apply') as apply, patch.object(lab,'create_secret') as create, \
                 self.assertRaises(RuntimeError):
                lab.prepare_database()
            oc.assert_not_called(); apply.assert_not_called(); create.assert_not_called()

    def test_existing_database_never_regenerates_missing_credential(self):
        with patch.object(lab,'database_resources',return_value=self.resources()), patch.object(lab,'get',return_value=None), \
             patch.object(lab,'create_secret') as create, self.assertRaisesRegex(RuntimeError,'refusing to regenerate'):
            lab.prepare_database()
        create.assert_not_called()

    def test_absent_database_uses_create_and_mirrored_image(self):
        resources = self.resources(); resources[2]['spec']['template']['spec']['containers'][0]['image'] = lab.PG_IMAGE
        with patch.object(lab,'database_resources',side_effect=[[None,None,None],resources]), patch.object(lab,'save'), \
             patch.object(lab,'create_secret'), patch.object(lab,'wait_ready'), patch.object(lab,'oc') as oc, patch.object(lab,'apply') as apply:
            self.assertEqual(lab.prepare_database()['action'],'CREATED')
        apply.assert_not_called()
        self.assertEqual([call.args[0] for call in oc.call_args_list],['create','create'])
        submitted = oc.call_args_list[-1].kwargs['data']
        container = submitted['items'][2]['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'],lab.PG_IMAGE)


class OptimizedRollout(unittest.TestCase):
    image = 'image-registry.openshift-image-registry.svc:5000/'+lab.NS+'/rhbk-optimized@sha256:'+'a'*64

    def resources(self):
        return {
            ('keycloak','rhbk'):{'metadata':{'name':'rhbk','uid':'kc-uid','generation':2},
                'spec':{'image':self.image,'startOptimized':True,'instances':1},
                'status':{'conditions':[{'type':'Ready','status':'True','observedGeneration':2}]}},
            ('statefulset','rhbk'):{'metadata':{'name':'rhbk','uid':'sts-uid','generation':3,
                'ownerReferences':[{'kind':'Keycloak','uid':'kc-uid'}]},
                'spec':{'replicas':1,'template':{'spec':{'containers':[{'name':'keycloak','image':self.image}]}}},
                'status':{'observedGeneration':3,'readyReplicas':1,'updatedReplicas':1,'currentRevision':'rhbk-r2','updateRevision':'rhbk-r2'}},
            ('pods',None):{'items':[{'metadata':{'name':'rhbk-0','uid':'pod-uid','labels':{'controller-revision-hash':'rhbk-r2'},
                'ownerReferences':[{'kind':'StatefulSet','uid':'sts-uid'}]},
                'spec':{'containers':[{'name':'keycloak','image':self.image}]},
                'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],
                    'containerStatuses':[{'name':'keycloak','ready':True,'imageID':self.image}]}}]}}

    def snapshot(self,resources):
        with patch.object(lab,'get',side_effect=lambda kind,name=None:copy.deepcopy(resources.get((kind,name)))):
            return lab.rhbk_rollout_snapshot(self.image)

    def test_missing_or_old_generation_never_accepts_ready(self):
        resources = self.resources(); status = resources[('keycloak','rhbk')]['status']
        del status['conditions'][0]['observedGeneration']
        self.assertIsNone(self.snapshot(resources))
        status['observedGeneration'] = 1; self.assertIsNone(self.snapshot(resources))
        status['observedGeneration'] = 2; self.assertEqual(self.snapshot(resources)['status'],'READY')
        status['conditions'][0]['observedGeneration'] = 1
        self.assertIsNone(self.snapshot(resources))

    def test_statefulset_revision_update_and_pod_identity_are_required(self):
        for field,value in [('observedGeneration',2),('readyReplicas',0),('updatedReplicas',0),('currentRevision','rhbk-r1')]:
            resources = self.resources(); resources[('statefulset','rhbk')]['status'][field] = value
            with self.subTest(field=field): self.assertIsNone(self.snapshot(resources))
        resources = self.resources(); resources[('pods',None)]['items'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        self.assertIsNone(self.snapshot(resources))

    def test_ready_pod_with_old_image_or_digest_is_rejected(self):
        resources = self.resources(); pod = resources[('pods',None)]['items'][0]
        pod['spec']['containers'][0]['image'] = 'old-image'
        self.assertIsNone(self.snapshot(resources))
        pod['spec']['containers'][0]['image'] = self.image
        pod['status']['containerStatuses'][0]['imageID'] = self.image[:-64]+'b'*64
        self.assertIsNone(self.snapshot(resources))

    def test_config_image_id_is_recorded_without_treating_it_as_manifest_digest(self):
        resources = self.resources()
        resources[('pods',None)]['items'][0]['status']['containerStatuses'][0]['imageID'] = 'cri-o://sha256:'+'b'*64
        result = self.snapshot(resources)
        self.assertEqual(result['pods'][0]['image_id_digest_check'],'NOT_COMPARABLE')
        self.assertEqual(result['pods'][0]['image'],self.image)

    def test_wait_records_only_completed_rollout_and_has_bounded_timeout(self):
        with patch.object(lab,'rhbk_rollout_snapshot',side_effect=[None,{'status':'READY','image':self.image}]), \
             patch.object(lab.time,'sleep'), patch.object(lab,'save') as save:
            lab.wait_rhbk_rollout(self.image,10)
        save.assert_called_once_with('evidence/rhbk-rollout.json',{'status':'READY','image':self.image})
        with patch.object(lab,'rhbk_rollout_snapshot',return_value=None), patch.object(lab.time,'monotonic',side_effect=[0,0,2]), \
             patch.object(lab.time,'sleep'), patch.object(lab,'save') as save, self.assertRaisesRegex(RuntimeError,'deadline'):
            lab.wait_rhbk_rollout(self.image,1)
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
