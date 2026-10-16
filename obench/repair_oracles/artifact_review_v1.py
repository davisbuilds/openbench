"""Bounded review artifacts: observe data and scope, never execute submitted source."""
import inspect
import json

SOURCE_PREFIX = ''
PROTOCOL = 'artifact-observations-v1'
ARTIFACT = 'submission/review.json'
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_FINDINGS = 64
GIT_POINTER = b'gitdir: /tmp/openbench-workspace.git\n'


def validate_request(request):
    if not isinstance(request, dict) or request != {'mode': 'review'}:
        raise ValueError('review request requires only mode=review')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def review_report(raw):
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('review artifact exceeds 64 KiB')
    value = strict_json(raw)
    if not isinstance(value, dict) or set(value) != {'findings'}:
        raise ValueError('review report requires only findings')
    findings = value['findings']
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ValueError('review findings must be a list of at most 64 entries')
    fields = {'title', 'body', 'file', 'line_start', 'line_end', 'priority'}
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != fields:
            raise ValueError('review finding requires title/body/file/line_start/line_end/priority')
        for key, limit in (('title', 512), ('body', 16000), ('file', 512)):
            if not isinstance(finding[key], str) or not finding[key].strip() or len(finding[key]) > limit:
                raise ValueError('review finding has invalid or excessive ' + key)
            finding[key].encode('utf-8')
        parts = finding['file'].split('/')
        if (finding['file'].startswith('/') or '\\' in finding['file']
                or any(p in ('', '.', '..') for p in parts) or '\x00' in finding['file']):
            raise ValueError('review finding file must be a relative workspace path')
        if (type(finding['priority']) is not int or not 0 <= finding['priority'] <= 3
                or type(finding['line_start']) is not int or type(finding['line_end']) is not int
                or not 1 <= finding['line_start'] <= finding['line_end'] <= 10000000):
            raise ValueError('review finding has invalid priority or line range')
    return findings


def observe(files, protected):
    """Observe report and changes outside submission/ and generated Git state."""
    import hashlib
    actual = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
              if not name.startswith(('submission/', '.git/'))
              and not (name == '.git' and data == GIT_POINTER)}
    drift = sorted('removed:' + n for n in protected.keys() - actual.keys())
    # Full review workspaces opt in through their sealed descriptor. Builds and
    # tests may create untracked files; changes to supplied bytes still fail.
    # Legacy selected-source packages retain their original strict scope rule.
    drift += sorted('added:' + n for n in actual.keys() - protected.keys()
                    if '.openbench-review.json' not in protected or n == '.git')
    drift += sorted('changed:' + n for n in actual.keys() & protected.keys() if actual[n] != protected[n])
    try:
        findings = review_report(files[ARTIFACT])
        return {'ok': True, 'value': {'findings': findings, 'scope_drift': drift}}
    except (KeyError, ValueError, UnicodeError, RecursionError) as exc:
        return {'ok': False, 'error': {'type': type(exc).__name__, 'message': str(exc)[:1024], 'operation': 'review'}}


def worker_program():
    # The fixed observer runs isolated with source bytes as data. Never import
    # a module, script, or executable from the submitted archive.
    definitions = '\n'.join(inspect.getsource(fn) for fn in (strict_json, review_report, observe))
    return ('import base64,hashlib,io,json,sys,tarfile\n'
            f'ARTIFACT={ARTIFACT!r}\nMAX_ARTIFACT_BYTES={MAX_ARTIFACT_BYTES}\nMAX_FINDINGS={MAX_FINDINGS}\nGIT_POINTER={GIT_POINTER!r}\n'
            + definitions + r'''
request = strict_json(sys.stdin.buffer.read(33554433))
files = {}
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['source'], validate=True))) as tar:
    for member in tar.getmembers():
        if (not member.isfile() or member.name in files or member.name.startswith('/')
                or any(p in ('', '.', '..') for p in member.name.split('/'))
                or '\\' in member.name or member.size > 2097152):
            raise ValueError('invalid artifact archive')
        files[member.name] = tar.extractfile(member).read()
        if len(files) > 4096 or sum(map(len, files.values())) > 16777216:
            raise ValueError('artifact archive exceeds its bound')
observed = observe(files, request['cases'][0]['protected_sha256'])
results = [observed for _ in request['cases']]
sys.stdout.write(json.dumps({'schema': 1, 'results': results}, allow_nan=False, ensure_ascii=False))
''')


def validate_runtime(image):
    from ..repair_worker import runtime_probe
    from ..sandbox_grading import GradingError
    value = runtime_probe(image, ['python3', '-I', '-c',
        "import json,sys;print(json.dumps({'python':list(sys.version_info[:2]),'observer':'artifact-review-v1'}))"])
    if (not isinstance(value, dict) or value.get('observer') != 'artifact-review-v1'
            or not isinstance(value.get('python'), list) or value['python'] < [3, 11]):
        raise GradingError('artifact observer requires Python 3.11 or newer')
    return value
