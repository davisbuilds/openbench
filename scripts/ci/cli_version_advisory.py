#!/usr/bin/env python3
"""Retain the weekly CLI report and upsert its issue when Issues are enabled.

Only status 1 (updates found) can write an issue. A disabled Issues feature is
an expected summary-only route; GitHub API errors remain failures. No pins,
runtimes, host CLIs or repository settings are changed.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

TITLE = 'CLI version bumps available'
MARKER = '<!-- openbench-cli-version-check -->'
GUIDANCE = '''Review releases and validate a new pinned runtime before adopting updates.
These pins describe the compatibility harness image. Repair sandboxes also pin Codex in
`docker/repair-sandbox/Dockerfile` and `obench/harbor_agents/sandbox_codex.py`.
Do not update a host CLI or runtime during a frozen campaign.

```bash
python3 -m obench.bump_clis --apply
docker build -t openbench-harness:latest obench/docker
```

This workflow reports only; it never changes pins or opens a pull request.'''


def report_body(report, status):
    if status == 1:
        description = 'The weekly upstream check found Dockerfile CLI pins behind npm’s latest published tags.'
    elif status == 0:
        description = 'No CLI pin updates found among successful registry lookups. Check skipped rows and warnings for unavailable coverage.'
    else:
        description = f'CLI comparison failed (exit {status}); no issue will be published.'
    # Registry output is untrusted text; indent it to prevent closing a Markdown fence.
    quoted = '\n'.join('    ' + line for line in report.strip().splitlines())
    return f'{MARKER}\n# CLI version advisory\n\n{description}\n\n{quoted}\n\n{GUIDANCE}\n'


class GitHub:
    def __init__(self, env):
        self.base = env.get('GITHUB_API_URL', 'https://api.github.com').rstrip('/')
        self.repo = env['GITHUB_REPOSITORY']
        self.token = env['GH_TOKEN']

    def request(self, method, suffix='', payload=None):
        request = urllib.request.Request(
            f'{self.base}/repos/{self.repo}{suffix}', method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': f'Bearer {self.token}',
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def upsert(self, body):
        metadata = self.request('GET')
        if not isinstance(metadata, dict) or type(metadata.get('has_issues')) is not bool:
            raise ValueError('repository response has no boolean has_issues')
        if not metadata['has_issues']:
            return 'Issues are disabled for this repository. Review the summary and `cli-version-advisory` artifact; no issue was created or updated.'
        page = 1
        while True:
            issues = self.request('GET', f'/issues?state=open&per_page=100&page={page}')
            if not isinstance(issues, list):
                raise ValueError('issue listing response is not a list')
            existing = next((issue for issue in issues
                             if 'pull_request' not in issue and issue.get('title') == TITLE
                             and MARKER in (issue.get('body') or '')), None)
            if existing:
                self.request('PATCH', f'/issues/{existing["number"]}', {'body': body})
                return f'Updated advisory issue #{existing["number"]}.'
            if len(issues) < 100:
                created = self.request('POST', '/issues', {'title': TITLE, 'body': body})
                return f'Created advisory issue: {created["html_url"]}.'
            page += 1


def main(argv=None, *, environ=None):
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--check-status', required=True, type=int)
    parser.add_argument('--advisory', default='cli-version-advisory.md')
    args = parser.parse_args(argv)
    body = report_body(Path(args.report).read_text(encoding='utf-8'), args.check_status)
    target = Path(args.advisory)
    # Retain the report before making any API request, including on delivery failure.
    target.write_text(body, encoding='utf-8')
    result = 0 if args.check_status in (0, 1) else 1
    if args.check_status == 1:
        try:
            note = GitHub(env).upsert(body)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            detail = f'HTTP {exc.code}' if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            note = f'Issue delivery failed ({detail}); the advisory remains available in this summary and the artifact.'
            print(note, file=sys.stderr)
            result = 1
        body += '\n' + note + '\n'
        target.write_text(body, encoding='utf-8')
    summary_path = env.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as summary:
            summary.write(body)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
