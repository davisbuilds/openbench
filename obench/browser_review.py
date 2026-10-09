"""Local, unscored browser review with a shared human/agent interaction protocol."""
from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import sys
import tarfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from .browser_policy import POLICY, identity, verify_options
from .browser_review_worker import DRIVER
from .sandbox_grading import bounded_command, source_archive

SCHEMA = 'openbench-browser-review-v1'
ACTIONS = ('snapshot', 'reset', 'click', 'fill', 'select', 'type', 'key', 'scroll', 'viewport', 'wait')
FIXTURES = ('everyday', 'stress', 'empty', 'loading', 'retry')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode()


def save(path, data):
    """Atomic private evidence; persistence failures propagate to the caller."""
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('xb') as f:
            os.chmod(tmp, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def fixture(name):
    if name not in FIXTURES:
        raise ValueError('unknown fixture')
    if name == 'stress':
        from .repair_oracles.activity_explorer import data
        rows = data(30)
        rows[-1].update(title='LongTitle' * 17 + 'abcdefg', project='LongProjectName' * 5 + 'ABCDE')
    else:
        titles = ['Review the release checklist', 'Investigate delayed background jobs',
                  'Improve keyboard navigation', 'Prepare the weekly summary',
                  'Fix mobile settings layout', 'Reconcile imported activity',
                  'Document the recovery procedure', 'Add search filter tests', 'Profile the dashboard query']
        rows = [dict(id=f'item-{i}', title=t, project=['Observatory', 'Ledger', 'Workspace'][i % 3],
                     status=['running', 'completed', 'failed'][i % 3],
                     description=f'{t}.\n\nCheck the current behavior, record the outcome, and leave a clear next step for the project owner.')
                for i, t in enumerate(titles)]
    return {'mode': name if name in ('loading', 'retry') else 'list', 'data': [] if name == 'empty' else rows}


def prepare(trials, output, *, keep_order=False):
    if not 1 <= len(trials) <= 26:
        raise ValueError('provide between 1 and 26 trials')
    entries = list(map(Path, trials))
    if not keep_order:
        secrets.SystemRandom().shuffle(entries)
    prepared, key = {}, {}
    for i, trial in enumerate(entries):
        receipt_bytes = (trial / 'verifier/sandbox-grading.json').read_bytes()
        receipt = json.loads(receipt_bytes)['grading']
        if receipt.get('oracle_id') != 'activity-explorer-v1':
            raise ValueError('review currently supports activity-explorer-v1 artifacts')
        expected = receipt.get('source_sha256')
        if not isinstance(expected, dict) or not expected or any(not k.startswith('web/') for k in expected):
            raise ValueError('missing browser source hashes')
        archive, hashes = source_archive(trial / 'artifacts/workspace/web', {k[4:] for k in expected})
        hashes = {'web/' + k: v for k, v in hashes.items()}
        if hashes != expected:
            raise ValueError('submission differs from grading receipt')
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            files = {'web/' + m.name: base64.b64encode(tar.extractfile(m).read()).decode() for m in tar}
        label = chr(65 + i)
        prepared[label] = {'files': files, 'source_sha256': hashes, 'grading_receipt_sha256': digest(receipt_bytes)}
        key[label] = {'trial': str(trial.resolve())}
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    bundle = {'schema': SCHEMA, 'candidates': prepared,
              'scope': 'unscored review; source bytes matched supplied grading receipts, not independent suite verification'}
    save(output / 'bundle.json', encoded(bundle))
    save(output / 'identity-key.json', encoded(key))
    return {'schema': SCHEMA, 'bundle': str(output.resolve()), 'candidates': list(prepared),
            'bundle_sha256': digest(encoded(bundle)), 'scope': bundle['scope']}


def load_bundle(root):
    raw = (Path(root) / 'bundle.json').read_bytes()
    if len(raw) > 80 * 1024 * 1024:
        raise ValueError('bundle size limit')
    bundle = json.loads(raw)
    if bundle.get('schema') != SCHEMA or not 1 <= len(bundle.get('candidates', {})) <= 26:
        raise ValueError('invalid review bundle')
    for label, entry in bundle['candidates'].items():
        if not re.fullmatch('[A-Z]', label) or set(entry['files']) != set(entry['source_sha256']):
            raise ValueError('invalid candidate entry')
        total = 0
        for name, value in entry['files'].items():
            if not name.startswith('web/') or '..' in Path(name).parts or Path(name).as_posix() != name:
                raise ValueError('unsafe asset name')
            content = base64.b64decode(value, validate=True)
            total += len(content)
            if digest(content) != entry['source_sha256'][name] or total > 2 * 1024 * 1024:
                raise ValueError('review asset hash or size mismatch')
    return bundle, digest(raw)


def viewport(value):
    if (not isinstance(value, dict) or set(value) != {'width', 'height'}
            or any(type(v) is not int for v in value.values())
            or not 360 <= value['width'] <= 1440 or not 640 <= value['height'] <= 1200):
        raise ValueError('viewport requires width 360..1440 and height 640..1200')
    return value


def validate_action(request):
    if not isinstance(request, dict) or request.get('action') not in ACTIONS:
        raise ValueError('unknown review action')
    action = request['action']
    fields = {'snapshot': set(), 'reset': {'candidate', 'fixture', 'viewport'},
              'click': {'ref', 'x', 'y'}, 'fill': {'ref', 'text'}, 'select': {'ref', 'value'},
              'type': {'text'}, 'key': {'key'}, 'scroll': {'x', 'y', 'delta'},
              'viewport': {'viewport'}, 'wait': {'ms'}}[action]
    if set(request) - fields - {'action', 'seq'}:
        raise ValueError('unexpected action fields')
    if action != 'snapshot' and (type(request.get('seq')) is not int or request['seq'] < 0):
        raise ValueError('an action requires the latest snapshot seq')
    if action in ('reset', 'viewport'):
        viewport(request.get('viewport'))
    if action == 'reset' and (not re.fullmatch('[A-Z]', str(request.get('candidate', '')))
                              or request.get('fixture') not in FIXTURES):
        raise ValueError('invalid candidate or fixture')
    if action in ('fill', 'select') or (action == 'click' and 'ref' in request):
        if type(request.get('ref')) is not int or not 0 <= request['ref'] < 150:
            raise ValueError('invalid control ref')
    if action == 'click' and 'ref' in request and ('x' in request or 'y' in request):
        raise ValueError('choose ref or coordinates')
    if action == 'scroll' or (action == 'click' and 'ref' not in request):
        for k in ('x', 'y'):
            if type(request.get(k)) is not int or not 0 <= request[k] < 1440:
                raise ValueError('invalid pointer coordinate')
    if action == 'scroll' and (type(request.get('delta')) is not int or not -1200 <= request['delta'] <= 1200):
        raise ValueError('invalid scroll delta')
    if action in ('fill', 'type', 'select'):
        field = 'value' if action == 'select' else 'text'
        if not isinstance(request.get(field), str) or len(request[field]) > 4000:
            raise ValueError('invalid text')
    if action == 'key' and request.get('key') not in ('Tab', 'Shift+Tab', 'Enter', 'Space', 'Escape', 'Backspace', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageDown', 'PageUp'):
        raise ValueError('unsupported key')
    if action == 'wait' and (type(request.get('ms')) is not int or not 0 <= request['ms'] <= 3000):
        raise ValueError('wait must be 0..3000ms')
    return request


class Browser:
    def __init__(self, image):
        if not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}', image):
            raise ValueError('browser image must be pinned by digest')
        self.name = 'obench-review-' + uuid.uuid4().hex
        self.process = None
        self.image = json.loads(bounded_command(['docker', 'image', 'inspect', image]))[0]['Id']
        try:
            bounded_command(['docker', 'create', '--name', self.name, '--network', 'none',
                '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--security-opt', 'seccomp=' + str(POLICY), '--read-only', '--pids-limit', '256',
                '--memory', '2048m', '--cpus', '1', '--shm-size', '256m',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=536870912,mode=1777',
                '--workdir', '/tmp', '--interactive', '--entrypoint', 'node', self.image, '-e', DRIVER])
            obj = json.loads(bounded_command(['docker', 'inspect', self.name]))[0]
            host = obj['HostConfig']
            if (obj['Image'] != self.image or obj['Config']['User'] != '10001:10001'
                    or host['NetworkMode'] != 'none' or not host['ReadonlyRootfs']
                    or host.get('Binds') or obj.get('Mounts') or host.get('PortBindings')
                    or host.get('CapDrop') != ['ALL'] or host.get('IpcMode') != 'private'
                    or not verify_options(host.get('SecurityOpt'))):
                raise ValueError('review container policy drift')
            self.process = subprocess.Popen(['docker', 'start', '--attach', '--interactive', self.name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except BaseException:
            self.close()
            raise

    def call(self, request):
        process = self.process
        payload = encoded(request)
        result, error = bytearray(), bytearray()
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as selector:
            for stream, event in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ), (process.stderr, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError('review browser timed out; session must restart')
                for key, _ in selector.select(.1):
                    stream = key.fileobj
                    if stream is process.stdin:
                        count = os.write(stream.fileno(), payload[:8192])
                        payload = payload[count:]
                        if not payload:
                            selector.unregister(stream)
                    else:
                        chunk = os.read(stream.fileno(), 8192)
                        if not chunk:
                            raise RuntimeError('review browser exited unexpectedly')
                        target = result if stream is process.stdout else error
                        target.extend(chunk)
                        if len(result) + len(error) > 8 * 1024 * 1024:
                            raise RuntimeError('review browser output limit')
                        if b'\n' in result:
                            return json.loads(result)

    def close(self):
        if self.process:
            self.process.stdin.close()
        result = subprocess.run(['docker', 'rm', '--force', self.name], capture_output=True, timeout=30)
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()
        if result.returncode:
            raise RuntimeError('review container cleanup failed: ' + self.name)


class Review:
    def __init__(self, bundle, bundle_hash, directory, browser):
        self.bundle, self.bundle_hash, self.directory, self.browser = bundle, bundle_hash, Path(directory), browser
        self.seq = 0
        self.current = {}
        self.last = None
        self.failed = False

    def act(self, request):
        validate_action(request)
        if self.failed:
            raise RuntimeError('session failed; restart with a new evidence directory')
        if request['action'] != 'snapshot' and request['seq'] != self.seq:
            raise ValueError('stale snapshot seq; inspect before retrying')
        if self.seq >= 1000:
            raise ValueError('session action limit; start a new session')
        command = dict(request)
        selection = dict(self.current)
        if command['action'] == 'reset':
            candidate = command['candidate']
            if candidate not in self.bundle['candidates']:
                raise ValueError('unknown candidate')
            selection = {k: command[k] for k in ('candidate', 'fixture', 'viewport')}
            command['files'] = self.bundle['candidates'][candidate]['files']
            command['fixture'] = fixture(command['fixture'])
        if not selection:
            raise ValueError('reset first')
        if command['action'] == 'viewport':
            selection['viewport'] = command['viewport']
        try:
            started = time.monotonic()
            result = self.browser.call(command)
            self.seq += 1
            stem = f'{self.seq:05d}'
            if result.get('ok'):
                self.current = selection
                png = base64.b64decode(result['value'].pop('screenshot'), validate=True)
                save(self.directory / (stem + '.png'), png)
                result['value']['screenshot'] = stem + '.png'
                result['value']['screenshot_sha256'] = digest(png)
            elif command['action'] in ('reset', 'viewport'):
                # The browser may have applied only part of a state transition.
                # No subsequent observation can claim a known selection.
                self.failed = True
            confirmed = not self.failed
            record = {'schema': SCHEMA, 'seq': self.seq, 'unscored': True, 'request': request,
                      'selection': dict(self.current) if confirmed else None,
                      'attempted_selection': selection, 'selection_confirmed': confirmed,
                      'bundle_sha256': self.bundle_hash,
                      'source_sha256': self.bundle['candidates'][self.current['candidate']]['source_sha256'] if confirmed else None,
                      'fixture_sha256': digest(encoded(fixture(self.current['fixture']))) if confirmed else None,
                      'image_id': self.browser.image, 'browser_policy_sha256': identity(),
                      'driver_sha256': digest(DRIVER.encode()), 'elapsed_s': round(time.monotonic() - started, 3),
                      'observed_at': time.time(), 'result': result, 'evidence': str(self.directory / (stem + '.json'))}
            save(self.directory / (stem + '.json'), encoded(record))
            self.last = record
            return record
        except BaseException:
            self.failed = True
            raise

    def status(self):
        return {'schema': SCHEMA, 'state': 'failed' if self.failed else 'ready', 'seq': self.seq,
                'candidates': list(self.bundle['candidates']), 'fixtures': list(FIXTURES),
                'actions': list(ACTIONS), 'last': self.last, 'unscored': True}


def serve(bundle_path, image, directory, port=0, ttl=7200):
    from .browser_review_ui import HTML, SCRIPT
    bundle, bundle_hash = load_bundle(bundle_path)
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    browser = Browser(image)
    review = Review(bundle, bundle_hash, directory, browser)
    stopped = False
    server = None
    descriptor = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def respond(self, status, data, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, auth=True):
            host = f'127.0.0.1:{server.server_port}'
            if self.headers.get('Host') != host or self.headers.get('Origin', 'http://' + host) != 'http://' + host:
                self.respond(403, encoded({'error': 'origin or host refused'}))
                return False
            if auth and not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.respond(403, encoded({'error': 'session authorization required'}))
                return False
            return True

        def do_GET(self):
            if self.path in ('/', '/app.js'):
                if self.allowed(False):
                    self.respond(200, (HTML if self.path == '/' else SCRIPT).encode(), 'text/html; charset=utf-8' if self.path == '/' else 'text/javascript')
                return
            if not self.allowed():
                return
            if self.path == '/status':
                return self.respond(200, encoded(review.status()))
            if re.fullmatch(r'/[0-9]{5}\.png', self.path):
                path = directory / self.path[1:]
                if path.is_file():
                    return self.respond(200, path.read_bytes(), 'image/png')
            self.respond(404, encoded({'error': 'unknown review resource'}))

        def do_POST(self):
            nonlocal stopped
            if not self.allowed():
                return
            try:
                size = int(self.headers.get('Content-Length', '-1'))
                if not 0 < size <= 65536 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('bounded JSON request required')
                data = json.loads(self.rfile.read(size))
                if self.path == '/stop':
                    stopped = True
                    self.respond(200, encoded({'state': 'stopping'}))
                elif self.path == '/act':
                    self.respond(200, encoded(review.act(data)))
                else:
                    self.respond(404, encoded({'error': 'unknown review operation'}))
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, encoded({'error': str(exc)[:1000], 'seq': review.seq}))
            except Exception as exc:
                stopped = True
                self.respond(500, encoded({'error': str(exc)[:1000], 'state': 'failed'}))

    def stop_signal(*_):
        nonlocal stopped
        stopped = True

    previous = {}
    try:
        server = HTTPServer(('127.0.0.1', port), Handler)
        server.timeout = .5
        review.act({'action': 'reset', 'candidate': next(iter(bundle['candidates'])),
                    'fixture': 'everyday', 'viewport': {'width': 1440, 'height': 900}, 'seq': 0})
        if not review.last['result'].get('ok'):
            raise RuntimeError('initial browser observation failed')
        descriptor = {'schema': SCHEMA, 'state': 'ready', 'url': f'http://127.0.0.1:{server.server_port}',
                      'token': token, 'pid': os.getpid(), 'container': browser.name,
                      'image_id': browser.image, 'bundle_sha256': bundle_hash,
                      'expires_at': time.time() + ttl}
        save(directory / 'session.json', encoded(descriptor))
        print(json.dumps({'schema': SCHEMA, 'state': 'ready', 'session': str(directory),
                          'viewer': descriptor['url'] + '/#' + token}), flush=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop_signal)
        while not stopped and not review.failed and time.time() < descriptor['expires_at']:
            server.handle_request()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if server:
            server.server_close()
        try:
            browser.close()
        except BaseException:
            if descriptor:
                descriptor['state'] = 'cleanup_failed'
                save(directory / 'session.json', encoded(descriptor))
            raise
        if descriptor:
            descriptor['state'] = 'failed' if review.failed else 'stopped'
            save(directory / 'session.json', encoded(descriptor))


def client(directory, endpoint='status', data=None):
    descriptor = json.loads((Path(directory) / 'session.json').read_text())
    if descriptor.get('state') != 'ready':
        raise ValueError('review session is ' + str(descriptor.get('state')))
    url = descriptor['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', url):
        raise ValueError('invalid loopback session URL')
    request = Request(url + '/' + endpoint, data=encoded(data) if data is not None else None,
                      headers={'Authorization': 'Bearer ' + descriptor['token'], 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=40) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            detail = json.loads(exc.read(8192))
        raise ValueError(detail.get('error', str(exc)) +
                         (f" (current seq: {detail['seq']})" if 'seq' in detail else '')) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Unscored, isolated interactive review. JSON output; no model calls or score writes.')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare', help='freeze receipt-matching web assets; random blind labels by default')
    p.add_argument('--trial', action='append', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--keep-order', action='store_true', help='retain existing A/B/C assignments; input order is not blinded')
    p = commands.add_parser('serve', help='run foreground; use tmux for a persistent review session')
    p.add_argument('bundle', type=Path)
    p.add_argument('--image', required=True)
    p.add_argument('--session', required=True, type=Path)
    p.add_argument('--port', default=0, type=int)
    p.add_argument('--ttl', default=7200, type=int, help='session lifetime seconds (60..28800)')
    for name in ('inspect', 'act', 'stop'):
        p = commands.add_parser(name)
        p.add_argument('session', type=Path)
        if name == 'act':
            p.add_argument('--request', required=True, help='JSON file or - for stdin; inspect gives current seq and controls')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.trial, args.output, keep_order=args.keep_order)
        elif args.command == 'serve':
            if not 60 <= args.ttl <= 28800 or not 0 <= args.port <= 65535:
                raise ValueError('invalid port or ttl')
            serve(args.bundle, args.image, args.session, args.port, args.ttl)
            return 0
        elif args.command == 'inspect':
            result = client(args.session)
        elif args.command == 'stop':
            result = client(args.session, 'stop', {})
        else:
            raw = sys.stdin.read(65537) if args.request == '-' else Path(args.request).read_text()
            if len(raw) > 65536:
                raise ValueError('request size limit')
            result = client(args.session, 'act', json.loads(raw))
        print(json.dumps(result))
        return 1 if result.get('result', {}).get('ok') is False else 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({'schema': SCHEMA, 'error': str(exc)[:1000]}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
