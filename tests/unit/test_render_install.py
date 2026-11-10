"""Installation input boundaries; the renderer never uses an ambient cluster."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('render_install', ROOT / 'scripts/render_install.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class RenderInstallTests(unittest.TestCase):
    def test_mutable_images_are_rejected(self):
        for key in install.IMAGE_PARAMETERS:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'pinned by sha256'):
                install.validate({key: 'registry.example.com/team/image:latest'})

    def test_both_architectures_and_mirror_digests_are_accepted(self):
        for arch in ('arm64', 'amd64'):
            install.validate({'ARCHITECTURE': arch, 'NAMESPACE': 'sso-demo',
                              'RHBK_BASE_IMAGE': 'mirror.example.com/team/rhbk@sha256:' + 'a' * 64})

    def test_invalid_target_or_hostname_is_rejected(self):
        for values in ({'NAMESPACE': 'ns/other'}, {'ARCHITECTURE': 'auto'},
                       {'RHBK_HOSTNAME': 'https://sso.example.com'}, {'RHBK_INSTANCES': '0'},
                       {'DB_PORT': '65536'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                install.validate(values)

    def test_parameter_file_cannot_contain_shell_expressions_or_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'params.env'
            for line in ('NAMESPACE=$(touch /tmp/should-not-run)', 'NAMESPACE=`uname`',
                         'NAMESPACE=demo;id', 'DB_PASSWORD=not-allowed',
                         'NAMESPACE=one\nNAMESPACE=two'):
                path.write_text(line)
                with self.subTest(line=line), self.assertRaises(ValueError):
                    install.read_parameters(path)

    def test_oc_processing_is_local_and_ignores_ambient_kubeconfig(self):
        result = type('Result', (), {'returncode': 0, 'stdout': json.dumps({
            'kind': 'List', 'items': [{'kind': 'Subscription'}]})})()
        template = {'parameters': [{'name': 'NAMESPACE', 'required': True}]}
        with patch.object(Path, 'read_text', return_value=json.dumps(template)), \
             patch.object(install.subprocess, 'run', return_value=result) as run:
            install.render_component('operator', {'NAMESPACE': 'sso-demo'})
        args = run.call_args.args[0]
        self.assertIn('--local=true', args)
        self.assertIn('--kubeconfig=' + install.os.devnull, args)
        self.assertEqual(args[0], 'oc')
        self.assertNotIn('apply', args)
        self.assertNotIn('create', args)

    def test_template_processing_error_does_not_write_a_partial_bundle(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(install, 'read_parameters', return_value={}), \
             patch.object(install, 'render_component', side_effect=[{'kind': 'List'}, ValueError('invalid')]):
            destination = Path(directory) / 'rendered'
            with self.assertRaisesRegex(ValueError, 'invalid'):
                install.render('unused.env', ['operator', 'build'], destination)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
