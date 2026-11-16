"""Matrix selection and aggregate gate must agree, including a deliberate empty matrix."""
import copy
import importlib.util
import json
import sys
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('harbor_ci', ROOT / 'scripts/ci/harbor_ci.py')
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
LANES = ('repair-grading', 'repair-tools', 'concurrency', 'browser', 'browser-quality')


class SelectionTests(unittest.TestCase):
    def test_every_lane_is_classified_for_pr_and_push(self):
        for event in ('pull_request', 'push'):
            with self.subTest(event=event):
                def decision(repo, base, head, merged, *, lane, comparison):
                    self.assertEqual((repo, base, head, merged, comparison),
                                     ('.', 'b', 'h', 'm' if event == 'pull_request' else None, event))
                    return {'run': lane == 'browser', 'reason': 'fixture'}
                with patch.object(ci.inputs, 'decide', side_effect=decision) as probe:
                    report = ci.classify('.', event, 'b', 'h', 'm')
                self.assertEqual(report['lanes'], ['browser'])
                self.assertEqual(report['mode'], 'classified')
                self.assertEqual(probe.call_count, 5)

    def test_any_lane_error_runs_every_lane_not_partial_result(self):
        def decision(*args, lane, **kwargs):
            if lane == 'concurrency':
                raise ValueError('unknown revision')
            return {'run': False}
        with patch.object(ci.inputs, 'decide', side_effect=decision):
            report = ci.classify('.', 'push', 'b', 'h')
        self.assertEqual(report['lanes'], list(LANES))
        self.assertEqual(report['mode'], 'fallback')
        self.assertIn('unknown revision', report['reason'])

    def test_manual_and_unknown_events_force_every_lane(self):
        for event in ('workflow_dispatch', 'future_event'):
            with patch.object(ci.inputs, 'decide') as probe:
                report = ci.classify('.', event)
            probe.assert_not_called()
            self.assertEqual(report['lanes'], list(LANES))
            self.assertEqual(report['mode'], 'forced')


def selection(lanes=()):
    return {'event': 'push', 'mode': 'classified', 'lanes': list(lanes),
            'decisions': {lane: {'run': lane in lanes} for lane in LANES}}


class GateTests(unittest.TestCase):
    def test_successful_selected_lanes_and_deliberate_skip_pass(self):
        ci.validate_gate(selection(['browser']), 'push', 'success', 'success')
        ci.validate_gate(selection(), 'push', 'success', 'skipped')
        for mode in ('fallback', 'forced'):
            ci.validate_gate({'event': 'workflow_dispatch', 'mode': mode, 'lanes': list(LANES)},
                             'workflow_dispatch', 'success', 'success')

    def test_selected_lane_failure_cancellation_or_skip_fails(self):
        for result in ('failure', 'cancelled', 'skipped', '', None):
            with self.subTest(result=result), self.assertRaises(ValueError):
                ci.validate_gate(selection(['browser']), 'push', 'success', result)

    def test_classification_failure_cannot_look_like_deliberate_skip(self):
        for result in ('failure', 'cancelled', 'skipped', ''):
            with self.subTest(result=result), self.assertRaises(ValueError):
                ci.validate_gate(selection(), 'push', result, 'skipped')

    def test_malformed_or_contradictory_decisions_fail(self):
        valid = selection(['browser'])
        malformed = [None, {}, {'event': 'push', 'mode': 'classified', 'lanes': []}]
        for key, value in (('lanes', ['browser', 'browser']), ('lanes', ['unknown']),
                           ('lanes', []), ('lanes', 'browser'), ('event', 'pull_request'),
                           ('mode', 'unknown'), ('mode', 'forced'), ('decisions', {})):
            item = copy.deepcopy(valid)
            item[key] = value
            malformed.append(item)
        item = copy.deepcopy(valid)
        item['decisions']['browser']['run'] = 'true'
        malformed.append(item)
        for report in malformed:
            with self.subTest(report=report), self.assertRaises(ValueError):
                ci.validate_gate(report, 'push', 'success', 'success')

    def test_empty_selection_is_only_valid_for_classified_pr_or_push(self):
        for event in ('workflow_dispatch', 'future_event'):
            report = selection()
            report['event'] = event
            with self.assertRaises(ValueError):
                ci.validate_gate(report, event, 'success', 'skipped')
        with self.assertRaises(ValueError):
            ci.validate_gate(selection(), 'push', 'success', 'success')


class WorkflowInterfaceTests(unittest.TestCase):
    def test_cli_failure_fallback_emits_full_matrix_and_gate_checks_result(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            summary = Path(directory) / 'summary'
            script = ROOT / 'scripts/ci/harbor_ci.py'
            selected = subprocess.run(
                [sys.executable, str(script), 'classify', '--repo', directory,
                 '--event', 'push', '--base', 'invalid', '--head', 'invalid',
                 '--github-output', str(output), '--summary', str(summary)],
                capture_output=True, text=True)
            self.assertEqual(selected.returncode, 0, selected.stderr)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(values['run'], 'true')
            self.assertEqual(json.loads(values['matrix']), list(LANES))
            self.assertEqual(json.loads(values['decision'])['mode'], 'fallback')
            self.assertIn('browser-quality: run', summary.read_text())
            for result, expected in (('success', 0), ('skipped', 1), ('failure', 1)):
                checked = subprocess.run(
                    [sys.executable, str(script), 'gate', '--event', 'push',
                     '--classify-result', 'success', '--integration-result', result,
                     '--decision', values['decision']], capture_output=True, text=True)
                self.assertEqual(checked.returncode, expected, checked.stdout + checked.stderr)

    def test_empty_matrix_has_safe_placeholder_but_gate_receives_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            ci.output_decision(selection(), output, None)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(values['run'], 'false')
            self.assertEqual(json.loads(values['matrix']), ['not-selected'])
            ci.validate_gate(json.loads(values['decision']), 'push', 'success', 'skipped')


if __name__ == '__main__':
    unittest.main()
