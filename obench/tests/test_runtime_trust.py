"""The CA detector sees its positive control and refuses empty evidence."""
from pathlib import Path
import unittest
from obench.runtime_trust import BLOCKED, inspect_bundle


class RuntimeTrustTests(unittest.TestCase):
    def test_known_incident_root_is_detected_among_duplicate_certificates(self):
        pem = (Path(__file__).resolve().parents[2]/
               'docker/repair-sandbox/trust/blocked-root.pem').read_bytes()
        self.assertEqual(inspect_bundle(pem+pem)['blocked'], [BLOCKED])
        self.assertEqual(inspect_bundle(pem+pem)['certificates'], 2)

    def test_other_root_does_not_match_the_incident_certificate(self):
        pem = (Path(__file__).resolve().parent/
               'fixtures/runtime-trust/allowed-root.pem').read_bytes()
        self.assertEqual(inspect_bundle(pem)['blocked'], [])
        self.assertEqual(inspect_bundle(pem)['certificates'], 1)

    def test_empty_or_non_certificate_input_is_not_a_clean_result(self):
        for data in (b'', b'no certificates'):
            with self.assertRaises(ValueError):
                inspect_bundle(data)
