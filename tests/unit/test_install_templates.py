"""Exercise the native template renderer without contacting a Kubernetes API."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ('operator', 'build', 'keycloak', 'postgres-demo')


def example(name):
    return dict(line.split('=', 1) for line in
                (ROOT / 'config' / ('rhbk-install.' + name + '.env')).read_text().splitlines()
                if line and not line.startswith('#'))


def fixture(name):
    values = example(name)
    values['RHBK_BASE_IMAGE'] = 'registry.example.test/rhbk@sha256:' + 'a' * 64
    values['RHBK_IMAGE'] = 'registry.example.test/optimized@sha256:' + 'b' * 64
    values['PG_IMAGE'] = 'registry.example.test/postgres@sha256:' + 'c' * 64
    values['PG_STORAGE_CLASS'] = 'test-storage-' + name
    return values


def process(template, values, overrides=(), success=True):
    with tempfile.TemporaryDirectory(prefix='rhbk-render-test-') as directory:
        parameters = Path(directory) / 'params.env'
        parameters.write_text(''.join(k + '=' + v + '\n' for k, v in values.items()))
        result = subprocess.run(
            ['oc', '--kubeconfig=' + os.devnull, 'process', '--local',
             '--ignore-unknown-parameters=true', '--param-file=' + str(parameters),
             '-f', str(ROOT / 'deploy' / 'install' / (template + '.json')), '-o', 'json',
             *[arg for value in overrides for arg in ('-p', value)]],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, 'KUBECONFIG': os.devnull})
    if not success:
        return result
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)['items']


def kind(items, value):
    return next(item for item in items if item['kind'] == value)


class TemplateSources(unittest.TestCase):
    def test_example_files_share_the_same_parameter_contract(self):
        self.assertEqual(set(example('crc')), set(example('openshift')))
        declared = set()
        for name in TEMPLATES:
            template = json.loads((ROOT / 'deploy/install' / (name + '.json')).read_text())
            self.assertEqual(template['apiVersion'], 'template.openshift.io/v1')
            declared.update(item['name'] for item in template['parameters'])
        self.assertLessEqual(declared, set(example('openshift')))
        self.assertFalse({'PASSWORD', 'TOKEN', 'CLIENT_SECRET'} & set(example('openshift')))

    def test_template_sources_have_no_crc_or_architecture_constants(self):
        for name in TEMPLATES:
            raw = (ROOT / 'deploy/install' / (name + '.json')).read_text()
            objects = json.loads(raw)['objects']
            rendered = json.dumps(objects)
            self.assertNotIn('crc.testing', rendered)
            self.assertNotIn('crc-csi-hostpath-provisioner', rendered)
            self.assertNotIn('rhbk-rotation-lab', rendered)
            self.assertNotIn('"arm64"', rendered)
            self.assertNotIn('"amd64"', rendered)
            self.assertTrue(all(obj['kind'] not in ('Secret', 'Namespace', 'CustomResourceDefinition',
                                                    'ClusterRole', 'ClusterRoleBinding') for obj in objects))


@unittest.skipUnless(shutil.which('oc'), 'OpenShift CLI is required for native local template rendering')
class NativeTemplateRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = {name: fixture(name) for name in ('crc', 'openshift')}
        cls.rendered = {name: {template: process(template, values) for template in TEMPLATES}
                        for name, values in cls.values.items()}

    def test_same_templates_render_both_environments(self):
        for name, templates in self.rendered.items():
            with self.subTest(environment=name):
                values = self.values[name]
                for items in templates.values():
                    self.assertNotRegex(json.dumps(items), r'\$\{')
                    self.assertTrue(all(obj['metadata']['namespace'] == values['NAMESPACE'] for obj in items))
                if name == 'openshift':
                    self.assertNotIn('crc.testing', json.dumps(templates))
                    self.assertNotIn('crc-csi-hostpath', json.dumps(templates))
                build = kind(templates['build'], 'BuildConfig')
                self.assertEqual(build['spec']['nodeSelector'], {'kubernetes.io/arch': values['ARCHITECTURE']})
                keycloak = kind(templates['keycloak'], 'Keycloak')
                affinity = keycloak['spec']['scheduling']['affinity']['nodeAffinity']
                self.assertEqual(affinity['requiredDuringSchedulingIgnoredDuringExecution']
                                 ['nodeSelectorTerms'][0]['matchExpressions'][0]['values'], [values['ARCHITECTURE']])

    def test_operator_is_namespace_scoped_and_manually_approved(self):
        for name, templates in self.rendered.items():
            values = self.values[name]
            group = kind(templates['operator'], 'OperatorGroup')
            subscription = kind(templates['operator'], 'Subscription')
            self.assertEqual(group['spec'], {'targetNamespaces': [values['NAMESPACE']]})
            self.assertEqual(subscription['spec']['installPlanApproval'], 'Manual')
            self.assertEqual(subscription['spec']['startingCSV'], values['OPERATOR_CSV'])
            self.assertEqual(subscription['spec']['source'], values['OPERATOR_CATALOG'])
            self.assertEqual(subscription['spec']['sourceNamespace'], values['OPERATOR_CATALOG_NAMESPACE'])

    def test_build_context_and_permissions_are_explicit(self):
        for name, templates in self.rendered.items():
            values = self.values[name]
            build = kind(templates['build'], 'BuildConfig')
            self.assertEqual(build['spec']['source'], {'type': 'Binary', 'binary': {}})
            self.assertEqual(build['spec']['triggers'], [])
            self.assertNotIn('postCommit', build['spec'])
            strategy = build['spec']['strategy']['dockerStrategy']
            self.assertEqual(strategy['dockerfilePath'], 'Dockerfile')
            self.assertEqual(strategy['buildArgs'], [{'name': 'RHBK_IMAGE', 'value': values['RHBK_BASE_IMAGE']}])
            self.assertEqual(build['spec']['output']['to'], {'kind': 'ImageStreamTag', 'name': values['BUILD_NAME'] + ':lab'})
            account = kind(templates['build'], 'ServiceAccount')
            self.assertEqual(build['spec']['serviceAccount'], account['metadata']['name'])
            bindings = {obj['roleRef']['name']: obj for obj in templates['build'] if obj['kind'] == 'RoleBinding'}
            self.assertEqual(bindings['system:image-builder']['subjects'],
                             [{'kind': 'ServiceAccount', 'name': account['metadata']['name'], 'namespace': values['NAMESPACE']}])
            self.assertEqual(bindings['system:image-puller']['subjects'][0]['name'],
                             'system:serviceaccounts:' + values['NAMESPACE'])

    def test_keycloak_uses_optimized_image_and_existing_secret_references(self):
        for name, templates in self.rendered.items():
            values = self.values[name]
            server = kind(templates['keycloak'], 'Keycloak')
            spec = server['spec']
            self.assertIs(spec['startOptimized'], True)
            self.assertEqual(spec['image'], values['RHBK_IMAGE'])
            self.assertIsInstance(spec['instances'], int)
            self.assertIsInstance(spec['db']['port'], int)
            self.assertEqual(spec['db']['host'], values['DB_HOST'])
            self.assertEqual(spec['db']['usernameSecret'], {'name': values['DB_USERNAME_SECRET'], 'key': values['DB_USERNAME_KEY']})
            self.assertEqual(spec['db']['passwordSecret'], {'name': values['DB_PASSWORD_SECRET'], 'key': values['DB_PASSWORD_KEY']})
            self.assertEqual(spec['bootstrapAdmin'], {'user': {'secret': values['BOOTSTRAP_SECRET']}})
            self.assertNotIn('features', spec)
            self.assertNotIn('unsupported', spec)
            route = kind(templates['keycloak'], 'Route')
            self.assertEqual(route['spec']['host'], values['RHBK_HOSTNAME'])
            self.assertEqual(route['spec']['to']['name'], server['metadata']['name'] + '-service')
            self.assertEqual(route['spec']['tls']['termination'], 'edge')
            self.assertIs(spec['http']['httpEnabled'], True)
            self.assertEqual(spec['proxy']['headers'], 'xforwarded')
            self.assertIs(spec['ingress']['enabled'], False)

    def test_completed_build_digest_overrides_placeholder(self):
        values = example('openshift')
        image = 'registry.example.test/team/from-completed-build@sha256:' + 'd' * 64
        server = kind(process('keycloak', values, ['RHBK_IMAGE=' + image]), 'Keycloak')
        self.assertEqual(server['spec']['image'], image)
        self.assertNotIn('REPLACE_WITH', server['spec']['image'])

    def test_optional_database_uses_configured_storage_and_secret_keys(self):
        for name, templates in self.rendered.items():
            values = self.values[name]
            items = templates['postgres-demo']
            pvc = kind(items, 'PersistentVolumeClaim')
            self.assertEqual(pvc['spec']['storageClassName'], values['PG_STORAGE_CLASS'])
            deployment = kind(items, 'Deployment')
            pod = deployment['spec']['template']['spec']
            self.assertEqual(pod['nodeSelector']['kubernetes.io/arch'], values['ARCHITECTURE'])
            container = pod['containers'][0]
            env = {item['name']: item for item in container['env']}
            self.assertNotIn('value', env['POSTGRESQL_PASSWORD'])
            self.assertEqual(env['POSTGRESQL_PASSWORD']['valueFrom']['secretKeyRef'],
                             {'name': values['DB_PASSWORD_SECRET'], 'key': values['DB_PASSWORD_KEY']})
            self.assertEqual(container['image'], values['PG_IMAGE'])
            self.assertFalse(any(obj['kind'] in ('Deployment', 'PersistentVolumeClaim') for obj in templates['keycloak']))

    def test_native_renderer_rejects_missing_required_parameters(self):
        for template, required in (('operator', 'NAMESPACE'), ('build', 'RHBK_BASE_IMAGE'),
                                   ('keycloak', 'RHBK_IMAGE'), ('postgres-demo', 'PG_STORAGE_CLASS')):
            values = self.values['openshift'].copy()
            del values[required]
            with self.subTest(template=template, required=required):
                result = process(template, values, success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(required, result.stderr)


if __name__ == '__main__':
    unittest.main()
