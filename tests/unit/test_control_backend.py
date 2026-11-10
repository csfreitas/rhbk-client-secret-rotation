"""Manual Jobs must inherit only a controller template owned by the selected environment."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
spec = importlib.util.spec_from_file_location('manual_control', ROOT/'scripts/control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ControlBackend(unittest.TestCase):
    def backend(self, owned=True):
        labels = {'lab.rhbk/owner': 'unit-owner', 'lab.rhbk/installation': 'unit-installation'}
        cron = {'metadata': {'labels': labels if owned else {}}, 'spec': {'jobTemplate': {'spec': {
            'template': {'spec': {'containers': [{'name': 'controller', 'env': [], 'args': ['evaluate']}]}}
        }}}}
        return SimpleNamespace(NS='selected-namespace', CRONJOB_NAME='selected-cron',
            guard=Mock(), get=Mock(return_value=cron), labels=lambda: labels,
            meta=lambda name: {'name': name, 'namespace': 'selected-namespace', 'labels': dict(labels)}, apply=Mock())

    def test_foreign_source_cron_never_creates_job(self):
        backend = self.backend(owned=False)
        with self.assertRaisesRegex(RuntimeError, 'belongs to another installation'):
            control.run('resume', wait=False, backend=backend)
        backend.apply.assert_not_called()

    def test_selected_backend_controls_namespace_and_cron(self):
        backend = self.backend()
        control.run('resume', wait=False, backend=backend)
        backend.guard.assert_called_once()
        backend.get.assert_called_once_with('cronjob', 'selected-cron')
        job = backend.apply.call_args.args[0]
        self.assertEqual(job['metadata']['namespace'], 'selected-namespace')
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['args'], ['resume'])
        self.assertEqual(job['metadata']['labels']['lab.rhbk/installation'], 'unit-installation')


if __name__ == '__main__':
    unittest.main()
