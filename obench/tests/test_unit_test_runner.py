"""The parallel unit runner must never pass a module that dropped tests."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('unit_tests_runner', ROOT / 'scripts/ci/unit_tests.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def report(returncode, output, expected):
    with contextlib.redirect_stdout(io.StringIO()):
        return runner.report('test_example', returncode, output, expected, 0.1, False, False)


class ReportTests(unittest.TestCase):
    def test_complete_module_passes_and_counts_skips(self):
        result = report(0, 'test_a ... ok\n\nRan 3 tests in 0.1s\n\nOK (skipped=1)\n', 3)
        self.assertIsNone(result['problem'])
        self.assertEqual((result['ran'], result['skipped']), (3, 1))

    def test_successful_exit_with_fewer_tests_than_discovered_fails(self):
        result = report(0, 'Ran 2 tests in 0.1s\n\nOK\n', 3)
        self.assertEqual(result['problem'], 'ran 2 of 3 discovered tests')

    def test_successful_exit_without_a_summary_fails(self):
        self.assertIsNotNone(report(0, 'no unittest summary\n', 1)['problem'])

    def test_last_summary_wins_over_test_output(self):
        # A test may print a nested runner's summary before the module's own.
        output = 'Ran 9 tests in 0.0s\n\nOK\n...\nRan 1 test in 0.2s\n\nOK\n'
        self.assertIsNone(report(0, output, 1)['problem'])

    def test_nonzero_exit_fails_even_when_counts_match(self):
        self.assertEqual(report(1, 'Ran 1 test in 0.1s\n\nFAILED (failures=1)\n', 1)['problem'], 'exit 1')

    def test_summary_fails_on_missing_module_results(self):
        results = [{'module': 'test_example', 'ran': 2, 'skipped': 0, 'elapsed': 0.1, 'problem': None}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.summarize(results, 3, 0.1), 1)
            self.assertEqual(runner.summarize(results, 2, 0.1), 0)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_matches_the_serial_loader(self):
        counts = runner.discover()
        self.assertIn('test_unit_test_runner', counts)
        suite = unittest.TestLoader().discover(str(ROOT / runner.START), top_level_dir=str(ROOT / runner.START))
        self.assertEqual(sum(counts.values()), suite.countTestCases())

    def test_module_command_is_the_serial_command_restricted_to_one_file(self):
        self.assertEqual(runner.module_command('test_x', verbose=False),
                         [*runner.SERIAL, '-p', 'test_x.py'])
