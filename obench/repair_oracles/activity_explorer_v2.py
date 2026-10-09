"""Activity explorer v2: isolated observations, equivalent detail semantics, diagnostics."""
from .activity_explorer import cases, contains_text
from ..browser_worker_v2 import program as worker_program


def reasons(name, value, request):
    failures = []
    if value.get('observer') != 'cdp-isolated-world-v2':
        return ['unprotected-observation']
    if name == 'keyboard':
        fields = {'statusFocused': 'status-not-keyboard-reachable',
                  'recordFocused': 'record-not-keyboard-reachable',
                  'detailOpened': 'details-not-keyboard-openable',
                  'closeFocused': 'close-not-keyboard-reachable',
                  'detailClosed': 'details-not-keyboard-closable'}
        return [reason for field, reason in fields.items() if value.get(field) is not True]
    expected = [r['title'] for r in request['data'] if
                request.get('query', '').lower() in (r['title'] + ' ' + r['project']).lower()
                and (not request.get('status') or r['status'] == request['status'].lower())]
    if value.get('titles') != expected:
        failures.append('record-content-or-order')
    if value.get('empty') != (not expected):
        failures.append('incorrect-empty-state')
    if value.get('error') is not False:
        failures.append('error-not-cleared')
    if request['mode'] == 'detail':
        if value.get('closed') is not True:
            failures.append('details-not-closable')
        if value.get('detailReadable') is not True:
            failures.append('details-not-readable')
        for field in ('title', 'project', 'status', 'description'):
            if not contains_text(value.get('detail'), request['data'][0][field], casefold=True):
                failures.append('missing-detail-' + field)
    if name == 'loading' and value.get('loading') is not True:
        failures.append('missing-loading-state')
    if name == 'retry':
        if value.get('retryFocused') is not True:
            failures.append('retry-not-keyboard-reachable')
        if value.get('requests') != 2:
            failures.append('incorrect-retry-requests')
    if request['mode'] == 'layout':
        if value.get('documentWidth', 10**6) > request['viewport']['width'] + 1:
            failures.append('horizontal-overflow')
        items = value.get('items', [])
        if len(items) != len(expected):
            failures.append('missing-layout-observations')
        for item, title in zip(items, expected):
            if not contains_text(item.get('text'), title, casefold=True):
                failures.append('missing-visible-title')
            for field, reason in (('clipped', 'clipped-content'), ('nestedScroll', 'nested-list-scrolling')):
                if item.get(field) is not False:
                    failures.append(reason)
            if item.get('hit') is not True:
                failures.append('covered-or-unreachable-control')
            if item.get('fontSize', 0) < 14:
                failures.append('text-too-small')
            box = item.get('box') or {}
            if box.get('width', 0) <= 0 or box.get('height', 0) < 24:
                failures.append('activity-target-too-small')
    return list(dict.fromkeys(failures))


def compare(name, value, request):
    return not reasons(name, value, request)


def grade(results):
    checks = []
    groups = {}
    for (name, bucket, request), result in zip(cases(), results, strict=True):
        try:
            failed = reasons(name, result['value'], request) if result.get('ok') is True else ['browser-operation-failed']
        except (KeyError, TypeError, ValueError):
            failed = ['invalid-observation']
        for reason in failed:
            groups.setdefault(reason, []).append(name)
        checks.append({'case': name, 'bucket': bucket, 'pass': not failed, 'failure_reasons': failed})
    buckets = {bucket: all(c['pass'] for c in checks if c['bucket'] == bucket) for _, bucket, _ in cases()}
    return {'score': sum(buckets.values()) / len(buckets), 'checks': checks, 'buckets': buckets,
            'oracle_version': 2, 'check_pass_fraction': sum(c['pass'] for c in checks) / len(checks),
            'all_checks_passed': all(c['pass'] for c in checks), 'failure_groups': groups,
            'scope': 'strict bucket score plus per-check diagnostics; neither measures design preference or percent implemented'}


def validate_runtime(image):
    from ..browser_worker_v2 import OBSERVER
    from ..repair_worker import run_worker
    from ..sandbox_grading import CandidateFailure, GradingError
    # Candidate-free preflight proves this installed Chromium supports the exact
    # CDP route before an observation failure can be classified as candidate-caused.
    code = "const {chromium}=require('/opt/browser-deps/node_modules/playwright');\n" + OBSERVER + r'''
(async()=>{
 const browser=await chromium.launch({chromiumSandbox:true});
 try {
  const page=await browser.newPage();
  await page.setContent('<button>Native control</button><script>window.observerCanary="page-world";window.getComputedStyle=()=>({fontSize:"9999px"});<\/script>');
  const observer=await Observer.create(page);
  const node=await observer.one('button','Native control');
  const clean=await observer.measure(node,el=>typeof observerCanary==='undefined' && getComputedStyle(el).fontSize!=='9999px');
  if(!clean)throw Error('observer world is not isolated');
  const value={playwright:require('/opt/browser-deps/node_modules/playwright/package.json').version,
   chromium:browser.version(),sandbox:true,observer:'cdp-isolated-world-v2',isolated_world:true};
  console.log(JSON.stringify({schema:1,results:[{ok:true,value}]}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1});
'''
    program = 'import subprocess,sys;sys.exit(subprocess.call(' + repr(['node', '-e', code]) + '))'
    try:
        result, _ = run_worker(image, b'', [{}], program=program, timeout=30, browser=True)
    except CandidateFailure as exc:
        raise GradingError('isolated browser observer preflight failed') from exc
    value = result[0].get('value', {})
    if (value.get('playwright') != '1.64.0' or value.get('sandbox') is not True
            or value.get('isolated_world') is not True or not value.get('chromium')):
        raise GradingError('isolated browser observer dependency mismatch')
    return value
