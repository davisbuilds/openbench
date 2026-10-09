"""Review evidence contracts; optional real Docker/browser journey via env image."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from obench.browser_review import (Browser, Review, client, encoded, fixture, load_bundle,
                                   prepare, validate_action)


def trial(root, *, hostile=False):
    from scripts.ci.browser_quality_fixtures import HTML, JS, CSS
    root = Path(root)
    web = root / 'artifacts/workspace/web'
    web.mkdir(parents=True)
    if hostile:
        JS += '''\nfetch('http://host.docker.internal:9999/host-canary').catch(()=>{});
fetch('file:///etc/passwd').catch(()=>{});
fetch('https://example.invalid/review-canary').catch(()=>{});
new WebSocket('ws://host.docker.internal:9999/socket');
fetch('/identity-key.json').then(r=>{document.body.dataset.keyStatus=r.status});
'''
    HTML = HTML.replace('id="close">Close details', 'id="close"><span aria-hidden="true">×</span> Close details')
    files = {'index.html': HTML, 'app.js': JS, 'style.css': CSS}
    hashes = {}
    for name, content in files.items():
        (web / name).write_text(content)
        hashes['web/' + name] = hashlib.sha256(content.encode()).hexdigest()
    verifier = root / 'verifier'
    verifier.mkdir()
    (verifier / 'sandbox-grading.json').write_text(json.dumps({'grading': {
        'oracle_id': 'activity-explorer-v1', 'source_sha256': hashes}}))
    return root


class ReviewContracts(unittest.TestCase):
    def test_prepare_freezes_and_checks_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = trial(root / 'trial')
            prepare([source], root / 'bundle')
            bundle, _ = load_bundle(root / 'bundle')
            self.assertNotIn(str(source), json.dumps(bundle))
            self.assertNotIn('model', bundle['candidates']['A'])
            self.assertEqual((root / 'bundle/identity-key.json').stat().st_mode & 0o777, 0o600)
            (source / 'artifacts/workspace/web/app.js').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'differs'):
                prepare([source], root / 'other')
            # Copy remains independent of the changed original.
            self.assertEqual(load_bundle(root / 'bundle')[0], bundle)

    def test_tampered_bundle_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare([trial(root / 'trial')], root / 'bundle')
            path = root / 'bundle/bundle.json'
            obj = json.loads(path.read_text())
            obj['candidates']['A']['files']['web/app.js'] = base64.b64encode(b'changed').decode()
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, 'hash'):
                load_bundle(root / 'bundle')

    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = trial(root / 'trial')
            file = source / 'artifacts/workspace/web/app.js'
            file.unlink()
            file.symlink_to(root / 'canary')
            (root / 'canary').write_text('private')
            with self.assertRaisesRegex(ValueError, 'path or type'):
                prepare([source], root / 'bundle')

    def test_authority_and_bounds_are_closed(self):
        for value in [
            {'action': 'evaluate', 'code': 'process.env'},
            {'action': 'snapshot', 'url': 'http://host.docker.internal'},
            {'action': 'click', 'seq': 1, 'ref': True},
            {'action': 'fill', 'seq': 1, 'ref': 0, 'text': 'x' * 4001},
            {'action': 'key', 'seq': 1, 'key': 'Control+L'},
            {'action': 'wait', 'seq': 1, 'ms': 1000000},
            {'action': 'click', 'seq': 1, 'x': float('nan'), 'y': 0},
            {'action': 'reset', 'seq': 1, 'candidate': 'A', 'fixture': 'everyday', 'viewport': {'width': 1, 'height': 800}},
            {'action': 'click', 'ref': 0},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_action(value)
        self.assertEqual(validate_action({'action': 'click', 'seq': 1, 'ref': 0})['ref'], 0)

    def test_stale_action_rejected_before_browser(self):
        r = Review({}, 'hash', Path('.'), None)
        r.seq = 2
        with self.assertRaisesRegex(ValueError, 'stale'):
            r.act({'action': 'click', 'seq': 1, 'ref': 0})

    def test_fixture_conditions_are_separate(self):
        everyday, stress = fixture('everyday'), fixture('stress')
        self.assertEqual(len(everyday['data']), 9)
        self.assertNotIn('LongUnbrokenIdentifier', json.dumps(everyday))
        self.assertEqual(len(stress['data']), 30)
        self.assertIn('LongUnbrokenIdentifier', json.dumps(stress))
        self.assertEqual(fixture('empty')['data'], [])
        self.assertEqual(fixture('retry')['mode'], 'retry')


@unittest.skipUnless(os.environ.get('OBENCH_BROWSER_IMAGE'), 'set OBENCH_BROWSER_IMAGE for real isolated browser review')
class ReviewBrowserJourney(unittest.TestCase):
    def test_shared_api_and_confinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepare([trial(root / 'trial', hostile=True)], root / 'bundle')
            session = root / 'session'
            with (root / 'server.log').open('w') as log:
                proc = subprocess.Popen([sys.executable, '-m', 'obench', 'review', 'serve', str(root / 'bundle'),
                    '--image', os.environ['OBENCH_BROWSER_IMAGE'], '--session', str(session), '--ttl', '120'], stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 40
                    while not (session / 'session.json').exists():
                        if proc.poll() is not None or time.monotonic() > deadline:
                            self.fail((root / 'server.log').read_text())
                        time.sleep(.1)
                    descriptor = json.loads((session / 'session.json').read_text())
                    status = client(session)
                    observed = status['last']['result']['value']
                    self.assertIn('Review the release checklist', observed['text'])
                    self.assertTrue(observed['runtime']['chromium_sandbox'])
                    self.assertTrue(any('host.docker.internal' in url for url in observed['blocked']))
                    self.assertTrue(any('example.invalid' in url for url in observed['blocked']))
                    # The only candidate origin serves selected static assets, never the identity key.
                    inspected = json.loads(subprocess.check_output(['docker', 'inspect', descriptor['container']]))[0]
                    self.assertEqual(inspected['HostConfig']['NetworkMode'], 'none')
                    self.assertFalse(inspected['Mounts'])
                    canary = root / 'host-only-canary'
                    canary.write_text('host-only')
                    read_probe = subprocess.check_output(['docker', 'exec', descriptor['container'], 'python3', '-c',
                        'from pathlib import Path; import json; print(json.dumps(dict(allowed=Path("/opt/browser-deps/node_modules/playwright/package.json").is_file(), denied=not Path(' + repr(str(canary)) + ').exists())))'])
                    self.assertEqual(json.loads(read_probe), {'allowed': True, 'denied': True})
                    hidden_probe = subprocess.check_output(['docker', 'exec', descriptor['container'], 'python3', '-c',
                        "from pathlib import Path; assert not any(Path(p).exists() for p in ['/tests','/solution','/opt/openbench/obench/repair_oracles','/opt/openbench/obench/browser_worker.py','/var/run/docker.sock']); print('hidden-paths-denied')"])
                    self.assertEqual(hidden_probe.strip(), b'hidden-paths-denied')
                    # Effective network denial from a subprocess in this same runtime.
                    network = subprocess.check_output(['docker', 'exec', descriptor['container'], 'python3', '-c',
                        "import socket,json; s=socket.socket();s.settimeout(1);print(json.dumps({'denied':s.connect_ex(('1.1.1.1',443))!=0}))"])
                    self.assertTrue(json.loads(network)['denied'])
                    # Same origin operations require the token; cross origin is refused even with it.
                    for headers in ({}, {'Authorization': 'Bearer ' + descriptor['token'], 'Origin': 'https://hostile.invalid'}):
                        with self.assertRaises(HTTPError) as failure:
                            urlopen(Request(descriptor['url'] + '/status', headers=headers))
                        self.assertEqual(failure.exception.code, 403)
                        failure.exception.close()
                    for resource in ('/identity-key.json', '/bundle.json', '/../../etc/passwd'):
                        with self.assertRaises(HTTPError) as failure:
                            urlopen(Request(descriptor['url'] + resource, headers={'Authorization': 'Bearer ' + descriptor['token']}))
                        self.assertEqual(failure.exception.code, 404)
                        failure.exception.close()
                    def act(action):
                        nonlocal status
                        result = client(session, 'act', {**action, 'seq': status['seq']})
                        self.assertTrue(result['result']['ok'], result)
                        status = client(session)
                        return result['result']['value']
                    search = next(c for c in observed['controls'] if c['name'] == 'Search activities')
                    value = act({'action': 'fill', 'ref': search['ref'], 'text': 'keyboard'})
                    self.assertIn('Improve keyboard navigation', value['text'])
                    self.assertNotIn('Review the release checklist', value['text'])
                    button = next(c for c in value['controls'] if c['name'] == 'Improve keyboard navigation')
                    value = act({'action': 'click', 'ref': button['ref']})
                    self.assertIn('Activity details', value['accessibility'])
                    close = next(c for c in value['controls'] if c['name'] == 'Close details')
                    value = act({'action': 'click', 'ref': close['ref']})
                    self.assertNotIn('region "Activity details"', value['accessibility'])
                    act({'action': 'viewport', 'viewport': {'width': 360, 'height': 800}})
                    act({'action': 'scroll', 'x': 180, 'y': 400, 'delta': 500})
                    for name, text in [('empty', 'No activities found'), ('retry', 'Could not load activities'), ('loading', 'Loading activities')]:
                        value = act({'action': 'reset', 'candidate': 'A', 'fixture': name, 'viewport': {'width': 360, 'height': 800}})
                        self.assertIn(text, value['text'])
                        if name == 'retry':
                            retry = next(c for c in value['controls'] if c['name'] == 'Retry')
                            value = act({'action': 'click', 'ref': retry['ref']})
                            self.assertIn('Review the release checklist', value['text'])
                    old = status['seq'] - 1
                    with self.assertRaisesRegex(ValueError, 'stale snapshot'):
                        client(session, 'act', {'action': 'key', 'key': 'Tab', 'seq': old})
                    record = status['last']
                    self.assertTrue(Path(record['evidence']).is_file())
                    self.assertTrue((session / record['result']['value']['screenshot']).is_file())
                    # A persistence error must be visible, and terminate the session.
                    (session / f"{status['seq'] + 1:05d}.png").mkdir()
                    with self.assertRaisesRegex(ValueError, 'directory|Directory'):
                        client(session, 'act', {'action': 'snapshot'})
                    proc.wait(timeout=35)
                    self.assertEqual(json.loads((session / 'session.json').read_text())['state'], 'failed')
                    self.assertNotEqual(subprocess.run(['docker', 'inspect', descriptor['container']], capture_output=True).returncode, 0)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=35)


if __name__ == '__main__':
    unittest.main()
