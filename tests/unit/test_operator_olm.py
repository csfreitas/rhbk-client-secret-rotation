"""OLM handoff safety; every Kubernetes request is mocked."""
import contextlib
import base64
import copy
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import lab
import operator_olm as olm


def metadata(name, namespace=lab.NS, uid=None):
    return {'name': name, 'namespace': namespace, 'uid': uid or name+'-uid', 'resourceVersion': '1',
            'labels': copy.deepcopy(lab.LABELS)}


def crd(name):
    return {'apiVersion': 'apiextensions.k8s.io/v1', 'kind': 'CustomResourceDefinition',
            'metadata': metadata(name, None), 'spec': {'group': 'k8s.keycloak.org', 'scope': 'Namespaced',
            'names': {'kind': 'Keycloak' if name.startswith('keycloaks.') else 'KeycloakRealmImport'},
            'versions': [{'name': 'v2beta1', 'served': True, 'storage': True}], 'conversion': {'strategy': 'None'}}}


def deployment():
    return {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {**metadata(olm.PACKAGE), 'generation': 1},
            'spec': {'replicas': 1, 'selector': {'matchLabels': {'name': olm.PACKAGE}},
                     'template': {'metadata': {'labels': {'name': olm.PACKAGE}, 'annotations': {'olm.targetNamespaces': lab.NS}},
                       'spec': {'serviceAccountName': olm.PACKAGE, 'containers': [{'name': olm.PACKAGE,
                         'image': 'registry.redhat.io/operator@sha256:'+'1'*64,
                         'env': [{'name': 'OPERATOR_NAME', 'value': olm.PACKAGE}]}]}}},
            'status': {'replicas': 1, 'observedGeneration': 1, 'availableReplicas': 1, 'updatedReplicas': 1}}


class Cluster:
    def __init__(self):
        resources = json.loads((ROOT/'deploy/operator/resources.json').read_text())['items']
        self.group, self.subscription = copy.deepcopy(resources)
        self.group['metadata'].update(uid='group-uid', resourceVersion='1')
        self.subscription['metadata'].update(uid='subscription-uid', resourceVersion='1')
        self.subscription['status'] = {'installPlanRef': {'name': 'install-pinned', 'uid': 'plan-uid'}}
        self.lock = {'operator': {'version': olm.VERSION, 'image': 'registry.redhat.io/operator@sha256:'+'1'*64},
                     'images': {'bundle': {'original': 'registry.redhat.io/bundle@sha256:'+'2'*64}}}
        self.dep = deployment()
        self.csv = {'apiVersion': 'operators.coreos.com/v1alpha1', 'kind': 'ClusterServiceVersion',
                    'metadata': metadata(olm.CSV), 'spec': {'version': olm.VERSION,
                    'installModes': [{'type': 'OwnNamespace', 'supported': True}],
                    'customresourcedefinitions': {'owned': [{'name': name} for name in sorted(olm.CRDS)]},
                    'install': {'spec': {'deployments': [{'name': olm.PACKAGE, 'spec': copy.deepcopy(self.dep['spec'])}],
                        'permissions': [{'serviceAccountName': olm.PACKAGE, 'rules': [
                            {'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'list', 'watch']}]}],
                        'clusterPermissions': [{'serviceAccountName': olm.PACKAGE, 'rules': [
                            {'apiGroups': ['config.openshift.io'], 'resources': ['ingresses'], 'verbs': ['get']}]}]}}}}
        self.crds = {name: crd(name) for name in olm.CRDS}
        self.plan = {'apiVersion': 'operators.coreos.com/v1alpha1', 'kind': 'InstallPlan',
                     'metadata': {**metadata('install-pinned', uid='plan-uid'), 'ownerReferences': [
                         {'kind': 'Subscription', 'name': olm.PACKAGE, 'uid': 'subscription-uid'}]},
                     'spec': {'approval': 'Manual', 'approved': False, 'clusterServiceVersionNames': [olm.CSV]},
                     'status': {'phase': 'RequiresApproval', 'plan': []}}
        self.rbac = [{'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': olm.PACKAGE}}]
        for field, kind, binding in [('permissions', 'Role', 'RoleBinding'), ('clusterPermissions', 'ClusterRole', 'ClusterRoleBinding')]:
            name = 'operator-'+kind.lower()+'-HASH'
            self.rbac.extend([
                {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': kind, 'metadata': {'name': name},
                 'rules': copy.deepcopy(self.csv['spec']['install']['spec'][field][0]['rules'])},
                {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': binding, 'metadata': {'name': name},
                 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': kind, 'name': name},
                 'subjects': [{'kind': 'ServiceAccount', 'name': olm.PACKAGE, 'namespace': lab.NS}]}])
        for resource in [self.csv, *self.crds.values(), *self.rbac]:
            self.plan['status']['plan'].append({'resolving': olm.CSV, 'resource': {
                'kind': resource['kind'], 'name': resource['metadata']['name'],
                'catalogSource': olm.SOURCE, 'catalogSourceNamespace': olm.SOURCE_NS, 'manifest': json.dumps(resource)}})
        self.cron = {'metadata': metadata('rotation-controller'), 'spec': {'suspend': True}}
        self.state = {'metadata': metadata('rotation-state'), 'data': {'state.json': json.dumps({'phase': 'COMPLETE', 'suspended': True})}}
        self.pods = []; self.jobs = []; self.other_csvs = []; self.other_groups = []; self.other_subs = []
        self.mutations = []; self.dryruns = []
        self.package = {'status': {'catalogSource': olm.SOURCE, 'catalogSourceNamespace': olm.SOURCE_NS,
                                  'channels': [{'name': olm.CHANNEL, 'currentCSV': olm.CSV}]}}
        self.bundle = None

    def get(self, kind, name=None, ns=lab.NS):
        if kind.startswith('packagemanifests'):return copy.deepcopy(self.package)
        if kind == 'customresourcedefinition':return copy.deepcopy(self.crds.get(name))
        if kind == 'operatorgroup' or kind == 'OperatorGroup':return copy.deepcopy(self.group)
        if kind == 'subscription' or kind == 'Subscription':return copy.deepcopy(self.subscription)
        if kind == 'installplan':return copy.deepcopy(self.plan)
        if kind == 'deployment':return copy.deepcopy(self.dep)
        if kind == 'clusterserviceversion':return copy.deepcopy(self.csv)
        if kind == 'cronjob':return copy.deepcopy(self.cron)
        if kind == 'configmap':return copy.deepcopy(self.bundle if ns == olm.SOURCE_NS else self.state)
        if kind == 'catalogsource':return {'metadata': {'uid': 'catalog-uid'}}
        if kind == 'jobs':return {'items': copy.deepcopy(self.jobs)}
        if kind == 'pods':return {'items': copy.deepcopy(self.pods)}
        raise AssertionError((kind, name, ns))

    def oc(self, *args, data=None):
        if args[0] == 'get' and '--all-namespaces' in args:
            values = {'operatorgroups.operators.coreos.com': ([self.group] if self.group else [])+self.other_groups,
                      'clusterserviceversions.operators.coreos.com': self.other_csvs,
                      'subscriptions.operators.coreos.com': self.other_subs}[args[1]]
            return json.dumps({'items': values})
        if args[0] == 'replace':
            assert '--dry-run=server' in args
            self.dryruns.append(copy.deepcopy(data)); result = copy.deepcopy(data)
            result['spec'].setdefault('conversion', {'strategy': 'None'})
            return json.dumps(result)
        self.mutations.append((args, copy.deepcopy(data)))
        if args[0] == 'create':
            value = copy.deepcopy(data); value['metadata'].update(uid='created-uid', resourceVersion='1')
            if value['kind'] == 'OperatorGroup':self.group = value
            else:self.subscription = value
            return json.dumps(value)
        if args[0] == 'patch' and args[1] == 'deployment':
            self.dep['spec']['replicas'] = 0; self.dep['status']['replicas'] = 0
            return '{}'
        if args[0] == 'patch' and args[1] == 'installplan':
            self.plan['spec']['approved'] = True
            return '{}'
        raise AssertionError(args)

    def referenced_bundle(self):
        objects = {"keycloak-operator.clusterserviceversion.yaml": self.csv,
                   **{name+'-v1.crd.yml': value for name, value in self.crds.items()}}
        self.bundle = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {**metadata('bundle-fixture', olm.SOURCE_NS),
            'annotations': {'olm.sourceImage': self.lock['images']['bundle']['original'], 'olm.contentEncoding': 'gzip+base64',
                            'operators.operatorframework.io.bundle.package.v1': olm.PACKAGE},
            'ownerReferences': [{'kind': 'CatalogSource', 'name': olm.SOURCE, 'uid': 'catalog-uid'}]},
            'binaryData': {name: base64.b64encode(base64.b64encode(gzip.compress(json.dumps(value).encode()))).decode()
                           for name, value in objects.items()}}
        reference = {'kind': 'ConfigMap', 'name': 'bundle-fixture', 'namespace': olm.SOURCE_NS,
                     'catalogSourceName': olm.SOURCE, 'catalogSourceNamespace': olm.SOURCE_NS, 'replaces': ''}
        self.plan['status']['plan'] = [{'resolving': olm.CSV, 'resource': {
            'kind': value['kind'], 'name': value['metadata']['name'], 'sourceName': olm.SOURCE,
            'sourceNamespace': olm.SOURCE_NS, 'manifest': json.dumps(reference)}}
            for value in [self.csv, *self.crds.values(), *olm.generated_rbac(self.csv)]]
        self.plan['status']['bundleLookups'] = [{'identifier': olm.CSV, 'path': self.lock['images']['bundle']['original'],
                                               'catalogSourceRef': {'name': olm.SOURCE, 'namespace': olm.SOURCE_NS}}]

    def peer(self, targets=None):
        ns = 'other-rhbk'
        other = copy.deepcopy(self.csv); other['metadata'] = metadata('rhbk-operator.v26.6.7-opr.3', ns)
        other['spec']['version'] = '26.6.7-opr.3'; self.other_csvs = [other]
        self.other_groups = [{'metadata': metadata('other-group', ns), 'spec': {'targetNamespaces': targets or [ns]}}]
        self.other_subs = [{'metadata': metadata(olm.PACKAGE, ns), 'spec': {'name': olm.PACKAGE, 'channel': olm.CHANNEL,
                         'installPlanApproval': 'Manual'}, 'status': {'installedCSV': other['metadata']['name'], 'currentCSV': other['metadata']['name']}}]


class OLMSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.cluster = Cluster()
        (self.root/'deploy/operator').mkdir(parents=True)
        (self.root/'deploy/operator/resources.json').write_text((ROOT/'deploy/operator/resources.json').read_text())
        (self.root/'versions.lock.yaml').write_text(json.dumps(self.cluster.lock))
        for target, replacement in [('ROOT', self.root), ('guard', lambda: None), ('get', self.cluster.get), ('oc', self.cluster.oc)]:
            handle = patch.object(lab, target, replacement); handle.start(); self.addCleanup(handle.stop)

    def test_prepare_creates_only_namespace_olm_resources_and_manual_approval(self):
        self.cluster.group = None; self.cluster.subscription = None
        self.assertEqual(olm.prepare()['status'], 'PREPARED')
        self.assertEqual([x[1]['kind'] for x in self.cluster.mutations], ['OperatorGroup', 'Subscription'])
        self.assertEqual(self.cluster.subscription['spec']['installPlanApproval'], 'Manual')
        self.assertFalse(self.cluster.plan['spec']['approved'])

    def test_prepare_is_idempotent_and_does_not_patch_existing_resources(self):
        olm.prepare(); olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_changed_catalog_head_blocks_before_creation(self):
        self.cluster.package['status']['channels'][0]['currentCSV'] = 'another-version'
        with self.assertRaisesRegex(RuntimeError, 'catalog channel'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_foreign_existing_subscription_is_never_adopted(self):
        self.cluster.subscription['metadata']['labels'] = {}
        with self.assertRaisesRegex(RuntimeError, 'not owned'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_other_disjoint_olm_namespace_is_allowed_and_unchanged(self):
        self.cluster.peer(); before = copy.deepcopy(self.cluster.other_subs)
        olm.prepare()
        self.assertEqual(self.cluster.other_subs, before)
        self.assertEqual(len(olm.verify_scope()['csvs']), 1)

    def test_other_operator_watching_lab_blocks(self):
        self.cluster.peer([lab.NS])
        with self.assertRaisesRegex(RuntimeError, 'another OperatorGroup'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_other_automatic_subscription_blocks_without_patching_it(self):
        self.cluster.peer(); self.cluster.other_subs[0]['spec']['installPlanApproval'] = 'Automatic'
        with self.assertRaisesRegex(RuntimeError, 'automatic updates'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_unrelated_og_intersection_without_keycloak_apis_is_not_rejected(self):
        self.cluster.other_groups = [{'metadata': metadata('monitoring', 'monitoring'), 'status': {'namespaces': ['']}}]
        self.assertEqual(olm.verify_scope(), {'csvs': [], 'subscriptions': []})

    def test_multiple_local_operatorgroups_block(self):
        self.cluster.other_groups = [copy.deepcopy(self.cluster.group)]
        with self.assertRaisesRegex(RuntimeError, 'multiple OperatorGroups'):olm.prepare()

    def test_static_operatorgroup_reserving_keycloak_apis_blocks_without_csv(self):
        self.cluster.other_groups = [{'metadata': {**metadata('reserved', 'other'), 'annotations': {
            'olm.providedAPIs': 'Keycloak.v2beta1.k8s.keycloak.org'}},
            'spec': {'staticProvidedAPIs': True}, 'status': {'namespaces': ['']}}]
        with self.assertRaisesRegex(RuntimeError, 'reserves Keycloak APIs'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_unexpected_csv_in_lab_blocks_even_if_it_has_other_apis(self):
        self.cluster.other_csvs = [{'metadata': metadata('unrelated-operator'), 'spec': {}}]
        with self.assertRaisesRegex(RuntimeError, 'another CSV exists'):olm.prepare()
        self.assertEqual(self.cluster.mutations, [])

    def test_plan_must_be_subscription_exact_reference_and_owner(self):
        for field in ('reference', 'owner', 'csv', 'catalog', 'image'):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.cluster.plan); sub = copy.deepcopy(self.cluster.subscription)
                if field == 'reference':sub['status']['installPlanRef']['uid'] = 'wrong'
                if field == 'owner':plan['metadata']['ownerReferences'][0]['uid'] = 'wrong'
                if field == 'csv':plan['spec']['clusterServiceVersionNames'].append('additional')
                if field == 'catalog':plan['status']['plan'][0]['resource']['catalogSource'] = 'other'
                if field == 'image':
                    manifest = json.loads(plan['status']['plan'][0]['resource']['manifest'])
                    manifest['spec']['install']['spec']['deployments'][0]['spec']['template']['spec']['containers'][0]['image'] = 'other'
                    plan['status']['plan'][0]['resource']['manifest'] = json.dumps(manifest)
                with self.assertRaises(RuntimeError):olm.validate_plan(plan, sub, self.cluster.lock)

    def test_plan_with_extra_resource_is_rejected(self):
        self.cluster.plan['status']['plan'].append({'resolving': olm.CSV, 'resource': {'kind': 'Secret'}})
        with self.assertRaisesRegex(RuntimeError, 'unexpected dependencies'):olm.approve('install-pinned', True)
        self.assertEqual(self.cluster.mutations, [])

    def test_plan_rbac_exactly_matches_csv_and_accepts_olm_hash_uppercase(self):
        definitions, dep = olm.validate_plan(self.cluster.plan, self.cluster.subscription, self.cluster.lock)
        self.assertEqual(len(definitions), 2)
        self.assertEqual(dep['name'], olm.PACKAGE)

    def test_extra_rules_subjects_namespace_and_role_refs_are_blocked(self):
        cases = [
            ('Role', lambda obj: obj['rules'][0]['verbs'].append('*')),
            ('Role', lambda obj: obj['metadata'].update(namespace='another-namespace')),
            ('RoleBinding', lambda obj: obj['subjects'][0].update(namespace='another-namespace')),
            ('RoleBinding', lambda obj: obj['subjects'].append({'kind': 'Group', 'name': 'system:authenticated'})),
            ('RoleBinding', lambda obj: obj['roleRef'].update(name='cluster-admin')),
            ('ClusterRole', lambda obj: obj.update(aggregationRule={'clusterRoleSelectors': [{}]})),
            ('ClusterRole', lambda obj: obj['rules'].append({'apiGroups': ['*'], 'resources': ['*'], 'verbs': ['*']})),
            ('ClusterRoleBinding', lambda obj: obj['subjects'][0].update(kind='User'))]
        for kind, mutate in cases:
            with self.subTest(kind=kind, mutate=mutate):
                plan = copy.deepcopy(self.cluster.plan)
                resource = next(step['resource'] for step in plan['status']['plan'] if step['resource']['kind'] == kind)
                obj = json.loads(resource['manifest']); mutate(obj); resource['manifest'] = json.dumps(obj)
                with self.assertRaises(RuntimeError):olm.validate_plan(plan, self.cluster.subscription, self.cluster.lock)
        self.assertEqual(self.cluster.mutations, [])

    def test_extra_rbac_resource_and_missing_binding_are_rejected(self):
        for extra in (True, False):
            plan = copy.deepcopy(self.cluster.plan)
            index = next(i for i, step in enumerate(plan['status']['plan']) if step['resource']['kind'] == 'RoleBinding')
            if extra:plan['status']['plan'].append(copy.deepcopy(plan['status']['plan'][index]))
            else:plan['status']['plan'].pop(index)
            with self.assertRaises(RuntimeError):olm.validate_plan(plan, self.cluster.subscription, self.cluster.lock)

    def test_reference_bundle_double_decoding_and_generated_rbac_are_validated(self):
        self.cluster.referenced_bundle()
        definitions, dep = olm.validate_plan(self.cluster.plan, self.cluster.subscription, self.cluster.lock)
        self.assertEqual(len(definitions), 2); self.assertEqual(dep['name'], olm.PACKAGE)
        review = json.loads((self.root/'evidence/operator-olm/bundle-review.json').read_text())
        self.assertEqual(len(review['manifest_sha256']), 3)
        self.assertEqual(self.cluster.mutations, [])

    def test_olm_deterministic_name_matches_observed_26_6_9_cluster_permission(self):
        permission = {'serviceAccountName': 'rhbk-operator', 'rules': [
            {'apiGroups': ['config.openshift.io'], 'resources': ['ingresses'], 'verbs': ['get']}]}
        self.assertEqual(olm.permission_name(permission, cluster=True),
                         'rhbk-operator.v26.6.9-op-bncG3Rw1ghJZWGmuV2TEiXpL7Tp32kugq5yFUT')

    def test_referenced_bundle_wrong_image_or_catalog_owner_is_rejected(self):
        for field in ('image', 'owner', 'file', 'lookup'):
            with self.subTest(field=field):
                self.cluster.referenced_bundle()
                if field == 'image':self.cluster.bundle['metadata']['annotations']['olm.sourceImage'] = 'different-digest'
                if field == 'owner':self.cluster.bundle['metadata']['ownerReferences'][0]['uid'] = 'another-catalog'
                if field == 'file':self.cluster.bundle['binaryData']['extra.yaml'] = 'ignored'
                if field == 'lookup':self.cluster.plan['status']['bundleLookups'] = []
                with self.assertRaises(RuntimeError):olm.validate_plan(self.cluster.plan, self.cluster.subscription, self.cluster.lock)
        self.assertEqual(self.cluster.mutations, [])

    def test_unknown_generated_rbac_name_in_reference_fails_closed(self):
        self.cluster.referenced_bundle()
        step = next(step for step in self.cluster.plan['status']['plan'] if step['resource']['kind'] == 'ClusterRole')
        step['resource']['name'] = 'unrelated-cluster-role'
        with self.assertRaisesRegex(RuntimeError, 'not derived'):olm.validate_plan(self.cluster.plan, self.cluster.subscription, self.cluster.lock)

    def test_yaml_parser_only_uses_client_dry_run_and_removes_temporary_file(self):
        with patch.object(lab, 'oc', return_value='{"kind":"ConfigMap"}') as oc:
            self.assertEqual(olm.yaml_object(b'kind: ConfigMap\n'), {'kind': 'ConfigMap'})
            args = oc.call_args.args
            self.assertIn('--dry-run=client', args); self.assertIn('--validate=false', args)
            self.assertFalse(Path(args[args.index('-f')+1]).exists())

    def test_empty_crd_review_cannot_report_success(self):
        with self.assertRaisesRegex(RuntimeError, 'exactly the two'):olm.verify_crds([])

    def test_changed_shared_crd_blocks_before_operator_stop_or_approval(self):
        self.cluster.peer()
        self.cluster.crds[next(iter(olm.CRDS))]['spec']['scope'] = 'Cluster'
        with self.assertRaisesRegex(RuntimeError, 'shared CRD specification'):olm.approve('install-pinned', True)
        self.assertEqual(self.cluster.mutations, [])
        self.assertTrue((self.root/'evidence/operator-olm/crd-review.json').exists())

    def test_api_defaults_are_normalized_without_a_real_crd_write(self):
        definitions = copy.deepcopy(list(self.cluster.crds.values()))
        for value in definitions:value['spec'].pop('conversion')
        self.assertTrue(all(x['spec_matches'] for x in olm.verify_crds(definitions)))
        self.assertEqual(len(self.cluster.dryruns), 2)
        self.assertEqual(self.cluster.mutations, [])

    def test_manual_deployment_needs_explicit_migration_flag(self):
        with self.assertRaisesRegex(RuntimeError, '--migrate-manual'):olm.approve('install-pinned')
        self.assertEqual(self.cluster.mutations, [])

    def test_suspended_terminal_state_and_no_active_jobs_are_required(self):
        cases = [('cron', lambda: self.cluster.cron['spec'].update(suspend=False)),
                 ('state', lambda: self.cluster.state['data'].update({'state.json': json.dumps({'phase': 'ROTATING', 'suspended': True})})),
                 ('job', lambda: self.cluster.jobs.append({'status': {'active': 1}})),
                 ('pending', lambda: self.cluster.jobs.append({'status': {}}))]
        for name, mutate in cases:
            with self.subTest(name=name):
                cron, state, jobs = copy.deepcopy((self.cluster.cron, self.cluster.state, self.cluster.jobs))
                mutate()
                with self.assertRaises(RuntimeError):olm.approve('install-pinned', True)
                self.assertEqual(self.cluster.mutations, [])
                self.cluster.cron, self.cluster.state, self.cluster.jobs = cron, state, jobs

    def test_handoff_snapshots_then_stops_only_manual_operator_then_approves(self):
        self.cluster.peer()
        self.assertEqual(olm.approve('install-pinned', True)['status'], 'APPROVED')
        self.assertEqual([(x[0][0], x[0][1]) for x in self.cluster.mutations], [('patch', 'deployment'), ('patch', 'installplan')])
        snapshot = json.loads((self.root/'evidence/operator-olm/manual-deployment.json').read_text())
        self.assertEqual(snapshot['deployment']['spec']['replicas'], 1)
        self.assertEqual(self.cluster.dep['spec']['replicas'], 0)
        self.assertEqual(self.cluster.other_subs[0]['status']['installedCSV'], 'rhbk-operator.v26.6.7-opr.3')
        for args, _ in self.cluster.mutations:
            self.assertEqual(args[args.index('-n')+1], lab.NS)
            patch_value = json.loads(args[args.index('-p')+1])
            self.assertTrue(any(x['path'] == '/metadata/uid' and x['op'] == 'test' for x in patch_value))

    def test_live_old_operator_pod_prevents_plan_approval(self):
        self.cluster.pods = [{'spec': {'serviceAccountName': olm.PACKAGE}, 'status': {'phase': 'Running'}}]
        with patch.object(olm.time, 'monotonic', side_effect=[0, 1, 100]), patch.object(olm.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'did not stop'):olm.approve('install-pinned', True)
        self.assertEqual(len(self.cluster.mutations), 1)
        self.assertFalse(self.cluster.plan['spec']['approved'])

    def test_manual_snapshot_rejects_unknown_secret_literal_without_recording_value(self):
        self.cluster.dep['spec']['template']['spec']['containers'][0]['env'].append({'name': 'AUTH_TOKEN', 'value': 'sensitive-fixture'})
        with self.assertRaisesRegex(RuntimeError, 'unknown literal'):olm.approve('install-pinned', True)
        self.assertEqual(self.cluster.mutations, [])
        self.assertFalse((self.root/'evidence/operator-olm/manual-deployment.json').exists())

    def test_changed_manual_selector_blocks_instead_of_deleting_deployment(self):
        self.cluster.dep['spec']['selector'] = {'matchLabels': {'other': 'deployment'}}
        with self.assertRaisesRegex(RuntimeError, 'selectors differ'):olm.approve('install-pinned', True)
        self.assertEqual(self.cluster.mutations, [])

    def test_repeating_approved_plan_never_stops_olm_deployment(self):
        olm.approve('install-pinned', True); self.cluster.mutations.clear()
        self.assertEqual(olm.approve('install-pinned', True)['status'], 'ALREADY_APPROVED')
        self.assertEqual(self.cluster.mutations, [])

    def test_new_install_does_not_require_a_manual_deployment_or_rotation_workload(self):
        self.cluster.dep = self.cluster.cron = self.cluster.state = None
        self.assertEqual(olm.approve('install-pinned')['status'], 'APPROVED')
        self.assertEqual([(x[0][0], x[0][1]) for x in self.cluster.mutations], [('patch', 'installplan')])

    def test_check_requires_csv_owner_ready_deployment_scope_and_exact_image(self):
        self.cluster.csv['status'] = {'phase': 'Succeeded'}
        self.cluster.subscription['status'].update(installedCSV=olm.CSV, currentCSV=olm.CSV)
        self.cluster.dep['metadata']['ownerReferences'] = [{'kind': 'ClusterServiceVersion', 'name': olm.CSV, 'uid': olm.CSV+'-uid'}]
        self.assertEqual(olm.check()['status'], 'PASS')
        self.cluster.dep['metadata']['ownerReferences'][0]['uid'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'not owned by the pinned CSV'):olm.check()

    def test_check_detects_changes_in_other_installation(self):
        self.cluster.peer(); olm.approve('install-pinned', True)
        self.cluster.dep['spec']['replicas'] = 1
        self.cluster.csv['status'] = {'phase': 'Succeeded'}
        self.cluster.subscription['status'].update(installedCSV=olm.CSV, currentCSV=olm.CSV)
        self.cluster.dep['metadata']['ownerReferences'] = [{'kind': 'ClusterServiceVersion', 'name': olm.CSV, 'uid': olm.CSV+'-uid'}]
        self.cluster.other_subs[0]['status']['installedCSV'] = 'unexpected-upgrade'
        with self.assertRaisesRegex(RuntimeError, 'another RHBK installation changed'):olm.check()

    def test_invalid_plan_name_never_calls_guard(self):
        with patch.object(lab, 'guard') as guard:
            with self.assertRaisesRegex(RuntimeError, 'invalid InstallPlan'):olm.approve('../another')
            guard.assert_not_called()


if __name__ == '__main__':unittest.main()
