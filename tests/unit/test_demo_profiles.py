"""Long validity must not turn a bounded expiration experiment into a years-long wait."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bounded_expiration', ROOT/'tests/integration/post_expiration.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
put_spec = importlib.util.spec_from_file_location('bounded_put', ROOT/'tests/integration/put_comparison.py')
put_module = importlib.util.module_from_spec(put_spec)
put_spec.loader.exec_module(put_module)


class DemoProfiles(unittest.TestCase):
    def test_long_profile_is_refused_before_any_client_mutation(self):
        api = Mock()
        api.admin.return_value = (json.loads((ROOT/'realm/profile.json').read_text()), {})
        experiment = module.Experiment(api, Mock())
        with self.assertRaisesRegex(module.Blocked, 'profile_mismatch'):
            experiment.verify_profile()
        api.admin.assert_called_once_with('GET', '/client-policies/profiles')
        api.create.assert_not_called()

    def test_short_profile_matches_bounded_experiment(self):
        api = Mock()
        api.admin.return_value = (json.loads((ROOT/'realm/profile-short.json').read_text()), {})
        module.Experiment(api, Mock()).verify_profile()
        api.create.assert_not_called()

    def test_years_long_wait_is_refused_even_if_selected_in_environment(self):
        with self.assertRaises(module.Blocked):
            module.Experiment(Mock(), Mock(), ttl=315360000)

    def test_server_deadline_cannot_make_short_profile_wait_for_years(self):
        experiment = module.Experiment(Mock(), Mock())
        with patch.object(module.time, 'time', return_value=1000), patch.object(module.time, 'sleep') as sleep:
            with self.assertRaisesRegex(module.Blocked, 'bounded_experiment_budget'):
                experiment.wait(1000+315360000)
            sleep.assert_not_called()

    def test_put_comparison_uses_the_same_profile_guard(self):
        api = Mock()
        api.admin.return_value = (json.loads((ROOT/'realm/profile.json').read_text()), {})
        with self.assertRaisesRegex(put_module.exp.Blocked, 'profile_mismatch'):
            put_module.Comparison(api, Mock()).verify_profile()
        api.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
