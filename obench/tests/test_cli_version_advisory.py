"""Exercise the workflow publisher against a local GitHub REST substitute."""
import contextlib
import http.server
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'cli_version_advisory', ROOT / 'scripts/ci/cli_version_advisory.py')
advisory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(advisory)


class TestCliVersionAdvisory(unittest.TestCase):
    def run_advisory(self, *, enabled=True, status=1, issues=None, failure=None):
        calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(length)) if length else None
                calls.append((self.command, self.path, payload))
                if failure and self.path == failure:
                    self.send_response(503)
                    response = {'message': 'service unavailable'}
                else:
                    self.send_response(200)
                    if self.path == '/repos/example/openbench':
                        response = {'has_issues': enabled}
                    elif self.command == 'GET':
                        response = issues.get(self.path, []) if isinstance(issues, dict) else issues or []
                    else:
                        response = {'html_url': 'https://github.com/example/openbench/issues/7'}
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            do_GET = do_POST = do_PATCH = handle_request

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / 'report.txt'
                report.write_text('codex 0.144.1 0.144.1 current\n' if status == 0
                                  else 'codex 0.144.1 0.144.2 behind\n')
                rendered = Path(tmp) / 'advisory.md'
                summary = Path(tmp) / 'summary.md'
                stdout, stderr = io.StringIO(), io.StringIO()
                env = {
                    'GITHUB_REPOSITORY': 'example/openbench',
                    'GITHUB_API_URL': f'http://127.0.0.1:{server.server_port}',
                    'GH_TOKEN': 'offline-test-token',
                    'GITHUB_STEP_SUMMARY': str(summary),
                }
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = advisory.main(['--report', str(report), '--check-status', str(status),
                                          '--advisory', str(rendered)], environ=env)
                return code, calls, rendered.read_text() if rendered.exists() else '', \
                    summary.read_text() if summary.exists() else '', stderr.getvalue()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_disabled_issues_keeps_advisory_and_never_calls_issue_endpoints(self):
        code, calls, rendered, summary, _ = self.run_advisory(enabled=False)
        self.assertEqual(code, 0)
        self.assertEqual([(method, url) for method, url, _ in calls],
                         [('GET', '/repos/example/openbench')])
        self.assertIn('Issues are disabled', rendered)
        self.assertIn('codex 0.144.1 0.144.2 behind', rendered)
        self.assertIn('frozen campaign', rendered)
        self.assertEqual(summary, rendered)

    def test_enabled_issues_creates_advisory_issue_and_retains_report(self):
        code, calls, rendered, summary, _ = self.run_advisory()
        self.assertEqual(code, 0)
        method, url, payload = calls[-1]
        self.assertEqual((method, url), ('POST', '/repos/example/openbench/issues'))
        self.assertEqual(payload['title'], 'CLI version bumps available')
        self.assertIn('codex 0.144.1 0.144.2 behind', payload['body'])
        self.assertIn('frozen campaign', payload['body'])
        self.assertIn('issues/7', rendered)
        self.assertEqual(rendered, summary)

    def test_enabled_issues_updates_only_marked_issue_excluding_pull_requests(self):
        issues = [
            {'title': advisory.TITLE, 'body': advisory.MARKER, 'number': 1, 'pull_request': {}},
            {'title': advisory.TITLE, 'body': 'unrelated issue', 'number': 2},
            {'title': advisory.TITLE, 'body': advisory.MARKER, 'number': 7},
        ]
        code, calls, _, _, _ = self.run_advisory(issues=issues)
        self.assertEqual(code, 0)
        self.assertEqual((calls[-1][0], calls[-1][1]),
                         ('PATCH', '/repos/example/openbench/issues/7'))
        self.assertFalse(any(method == 'POST' for method, _, _ in calls))

    def test_enabled_issues_finds_existing_advisory_on_later_page(self):
        prefix = '/repos/example/openbench/issues?state=open&per_page=100&page='
        pages = {
            prefix + '1': [{'title': 'Unrelated', 'number': n} for n in range(100)],
            prefix + '2': [{'title': advisory.TITLE, 'body': advisory.MARKER, 'number': 7}],
        }
        code, calls, _, _, _ = self.run_advisory(issues=pages)
        self.assertEqual(code, 0)
        self.assertIn(('GET', prefix + '2', None), calls)
        self.assertEqual((calls[-1][0], calls[-1][1]),
                         ('PATCH', '/repos/example/openbench/issues/7'))
        self.assertFalse(any(method == 'POST' for method, _, _ in calls))

    def test_no_updates_writes_summary_without_any_github_requests(self):
        code, calls, rendered, summary, _ = self.run_advisory(status=0)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn('No CLI pin updates found', rendered)
        self.assertIn('codex', rendered)
        self.assertEqual(rendered, summary)

    def test_repository_api_failure_fails_and_preserves_report(self):
        code, calls, rendered, summary, stderr = self.run_advisory(
            failure='/repos/example/openbench')
        self.assertNotEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('Issue delivery failed', rendered)
        self.assertIn('codex', rendered)
        self.assertEqual(rendered, summary)
        self.assertIn('503', stderr)

    def test_issue_api_failure_is_not_treated_as_disabled_issues(self):
        code, calls, rendered, summary, _ = self.run_advisory(
            failure='/repos/example/openbench/issues?state=open&per_page=100&page=1')
        self.assertNotEqual(code, 0)
        self.assertIn('Issue delivery failed', rendered)
        self.assertNotIn('Issues are disabled', rendered)
        self.assertFalse(any(method in ('PATCH', 'POST') for method, _, _ in calls))
        self.assertEqual(rendered, summary)

    def test_failed_comparison_retains_error_without_publishing_issue(self):
        code, calls, rendered, summary, _ = self.run_advisory(status=2)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn('CLI comparison failed', rendered)
        self.assertEqual(rendered, summary)


if __name__ == '__main__':
    unittest.main()
