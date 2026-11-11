"""Browser preflights admit exact qualified graphs and reject unknown versions."""
import unittest
from unittest.mock import patch

from obench.repair_oracles import activity_explorer, activity_explorer_v2
from obench.sandbox_grading import GradingError


class BrowserRuntimeVersionTests(unittest.TestCase):
    def test_preflights_accept_supported_upstream_and_historical_graphs(self):
        for oracle in (activity_explorer, activity_explorer_v2):
            for version in ('1.64.0', '1.65.0'):
                with self.subTest(oracle=oracle.__name__, version=version):
                    value = {'playwright': version, 'chromium': '158.0.8127.18',
                             'sandbox': True, 'isolated_world': True,
                             'observer': 'cdp-isolated-world-v2'}
                    with patch('obench.repair_worker.run_worker', return_value=([{'ok': True, 'value': value}], {})):
                        self.assertEqual(oracle.validate_runtime('image'), value)

    def test_preflights_reject_unknown_graphs(self):
        for oracle in (activity_explorer, activity_explorer_v2):
            for version in ('1.63.0', '1.66.0', None):
                with self.subTest(oracle=oracle.__name__, version=version):
                    value = {'playwright': version, 'chromium': '158.0.8127.18',
                             'sandbox': True, 'isolated_world': True}
                    with patch('obench.repair_worker.run_worker', return_value=([{'ok': True, 'value': value}], {})):
                        with self.assertRaises(GradingError):
                            oracle.validate_runtime('image')
