"""Fresh-checkout installation boundaries. All subprocess/cluster calls are mocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import lab


def load(name):
    spec = importlib.util.spec_from_file_location('public_'+name, ROOT/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load('setup')
mirror = load('mirror')


def crd(name='keycloaks.k8s.keycloak.org'):
    fields = {'startOptimized', 'features', 'bootstrapAdmin', 'image', 'instances', 'db'}
    return {'kind': 'CustomResourceDefinition', 'metadata': {'name': name},
            'spec': {'group': 'k8s.keycloak.org', 'scope': 'Namespaced', 'names': {'kind': 'Keycloak'},
                     'versions': [{'name': 'v2beta1', 'served': True, 'schema': {'openAPIV3Schema': {
                         'properties': {'spec': {'properties': {key: {} for key in fields}}}}}}]},
            'status': {'conditions': [{'type': 'Established', 'status': 'True'}]}}


class SetupSafety(unittest.TestCase):
    def test_missing_dedicated_kubeconfig_never_calls_ambient_oc(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(lab, 'PRIVATE', Path(directory)), patch.object(lab.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'dedicated CRC kubeconfig'):
                lab.oc('get', 'namespaces')
            run.assert_not_called()

    def test_kubeconfig_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source').write_text('fixture')
            (root/'crc.kubeconfig').symlink_to(root/'source')
            with patch.object(lab, 'PRIVATE', root), self.assertRaisesRegex(RuntimeError, 'dedicated CRC kubeconfig'):
                lab.kubeconfig_args()

    def test_configure_rejects_non_crc_before_export_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.write_text('fixture')
            with patch.object(lab, 'PRIVATE', root/'.private'), patch.object(setup, 'run', return_value=b'https://api.example.invalid:6443') as run:
                with self.assertRaisesRegex(RuntimeError, 'not the local CRC'):
                    setup.configure(source, 'explicit-context')
                self.assertEqual(run.call_count, 1)
                self.assertFalse((root/'.private').exists())

    def test_configure_embeds_only_explicit_context_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.write_text('fixture')
            with patch.object(lab, 'PRIVATE', root/'.private'), patch.object(setup, 'run', side_effect=[b'https://api.crc.testing:6443', b'flattened-fixture']) as run, contextlib.redirect_stdout(io.StringIO()):
                setup.configure(source, 'chosen-context')
                self.assertEqual((root/'.private/crc.kubeconfig').read_bytes(), b'flattened-fixture')
                self.assertEqual((root/'.private/crc.kubeconfig').stat().st_mode & 0o777, 0o600)
                self.assertIn('--context=chosen-context', run.call_args.args[0])
                self.assertIn('--minify', run.call_args.args[0])

    def test_setup_missing_registry_auth_cannot_mutate_cluster(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(lab, 'guard'), patch.object(setup, 'ensure_namespace') as namespace, patch.object(setup, 'prepare_bundle') as bundle:
            with self.assertRaisesRegex(RuntimeError, 'REGISTRY_AUTH_FILE'):
                setup.install()
            namespace.assert_not_called(); bundle.assert_not_called()

    def test_setup_never_replaces_existing_compatible_crd(self):
        obj = crd(); name = obj['metadata']['name']
        with patch.object(lab, 'get', return_value=obj), patch.object(lab, 'oc') as oc:
            self.assertEqual(setup.install_missing_crds({name: (obj, obj)}), [])
            oc.assert_not_called()

    def test_setup_creates_absent_crd_instead_of_apply(self):
        obj = crd(); name = obj['metadata']['name']
        with patch.object(lab, 'get', return_value=obj), patch.object(lab, 'oc') as oc:
            self.assertEqual(setup.install_missing_crds({name: (obj, None)}), [name])
            oc.assert_called_once_with('create', '-f', '-', data=obj)

    def test_unsupported_existing_crd_blocks(self):
        obj = crd(); obj['spec']['versions'][0]['served'] = False
        with self.assertRaisesRegex(RuntimeError, 'does not serve v2beta1'):
            setup.validate_crd(obj, obj['metadata']['name'])

    def test_missing_start_optimized_blocks_without_schema_replacement(self):
        obj = crd(); del obj['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']['properties']['startOptimized']
        with self.assertRaisesRegex(RuntimeError, 'schema lacks fields'):
            setup.validate_crd(obj, obj['metadata']['name'])

    def test_bundle_extract_never_writes_arbitrary_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root/'bundle.tar'; target = root/'output'; target.mkdir()
            with tarfile.open(archive, 'w') as stream:
                for name in ['../../escape', 'manifests/keycloak-operator.clusterserviceversion.yaml']:
                    entry = tarfile.TarInfo(name); entry.size = 2; stream.addfile(entry, io.BytesIO(b'{}'))
            setup.extract_members(archive, target)
            self.assertEqual([p.name for p in target.iterdir()], ['keycloak-operator.clusterserviceversion.yaml'])

    def test_bundle_symlink_in_required_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root/'bundle.tar'
            with tarfile.open(archive, 'w') as stream:
                entry = tarfile.TarInfo('manifests/keycloak-operator.clusterserviceversion.yaml'); entry.type = tarfile.SYMTYPE; entry.linkname = '/elsewhere'; stream.addfile(entry)
            with self.assertRaisesRegex(RuntimeError, 'invalid bundle manifest'):
                setup.extract_members(archive, root)

    def test_stale_bundle_files_are_removed_even_when_download_cannot_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root/'bundle'; target.mkdir()
            for name in setup.BUNDLE_FILES:(target/name).write_text('stale')
            with patch.object(lab, 'PRIVATE', root), patch.object(setup.shutil, 'which', return_value=None), self.assertRaisesRegex(RuntimeError, 'skopeo or a working podman'):
                setup.extract_bundle('registry.invalid/bundle@sha256:'+'1'*64, root/'auth', target)
            self.assertFalse(any(target.iterdir()))

    def test_guard_does_not_adopt_existing_lab_by_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def get(kind, name, ns):
                if kind == 'node':return {'metadata': {'name': 'crc'}, 'status': {'nodeInfo': {'architecture': 'arm64'}}}
                if name == 'kube-system':return {'metadata': {'uid': 'test-cluster'}}
                return {'metadata': {'name': lab.NS, 'uid': 'another-lab', 'labels': lab.LABELS}}
            with patch.object(lab, 'ROOT', root), patch.object(lab, 'oc', return_value='https://api.crc.testing:6443'), patch.object(lab, 'get', get), self.assertRaisesRegex(RuntimeError, 'not created by this checkout'):
                lab.guard(False)

    def test_guard_rejects_matching_namespace_uid_from_another_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'evidence').mkdir()
            (root/'evidence/namespace-identity.json').write_text(json.dumps({'name': lab.NS, 'uid': 'test-namespace', 'cluster_uid': 'old-cluster'}))
            def get(kind, name, ns):
                if kind == 'node':return {'metadata': {'name': 'crc'}, 'status': {'nodeInfo': {'architecture': 'arm64'}}}
                if name == 'kube-system':return {'metadata': {'uid': 'new-cluster'}}
                return {'metadata': {'name': lab.NS, 'uid': 'test-namespace', 'labels': lab.LABELS}}
            with patch.object(lab, 'ROOT', root), patch.object(lab, 'oc', return_value='https://api.crc.testing:6443'), patch.object(lab, 'get', get), self.assertRaisesRegex(RuntimeError, 'not created by this checkout'):
                lab.guard(False)

    def test_mirror_replaces_mutable_tag_with_fixed_arm64_digest(self):
        digest = 'sha256:'+'1'*64
        self.assertEqual(mirror.source_image({'original': 'registry.redhat.io/example/image:latest', 'digest_arm64': digest}), 'registry.redhat.io/example/image@'+digest)


if __name__ == '__main__':
    unittest.main()
