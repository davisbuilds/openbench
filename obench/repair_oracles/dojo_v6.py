"""Dojo v6: representative locators and observable identity invariants.

Operator-only. Neither this module nor its expected values enters the solver.
Versions 3--5 retain their original fixtures and scoring semantics.
"""
import json

from .. import sandbox_grading as legacy

IDS = (
    'native-listing', 'ignore-user', 'ignore-assistant', 'ignore-tool',
    'ignore-compaction', 'absent-listing', 'empty-listing', 'ordinary-instruction',
    'declared-mode', 'unsupported-exec', 'unsupported-codex-exec', 'missing-mode',
    'direct-missing-mode', 'direct-unsupported-mode', 'direct-declared-mode',
    'alias-version-equivalence', 'removed-origin', 'removed-duplicates',
    'replaced-origin', 'equal-empty', 'equal-single', 'equal-duplicates',
    'permutation-equivalence', 'added-duplicates', 'reverse-origin-replacement',
    'bundled-connector-replacement', 'added-entry', 'removed-entry',
    'renamed-entry', 'alias-equivalence', 'version-equivalence',
)


def cases():
    result = []
    for name, (bucket, request, expected) in zip(IDS, legacy.dojo_cases(oracle_version=5), strict=True):
        encoded = json.dumps(request)
        for version in ('1', '2'):
            encoded = encoded.replace(f'/demo/{version}/review/',
                                      f'/demo/{version}.0.0/skills/review/')
        result.append((name, bucket, json.loads(encoded), expected))
    plugin = '/synthetic/.codex/plugins/cache/openai-curated-remote/demo/1.0.0/skills/review/SKILL.md'
    def pair(name, left, right, expected):
        result.append((name, 'mismatch', {
            'op': 'mismatch', 'live': legacy.block([('review', left)]),
            'recorded': [legacy.record('developer', legacy.block([('review', right)]))],
        }, expected))
    pair('different-plugin', plugin, plugin.replace('/demo/', '/other/'), (1, 1, 1, 1))
    pair('different-skill-file', plugin, plugin.replace('/skills/review/', '/skills/other/'), (1, 1, 1, 1))
    pair('different-marketplace', plugin, plugin.replace('openai-curated-remote', 'other-market'), (1, 1, 1, 1))
    pair('nonplugin-version-directory', '/synthetic/projects/1.0.0/skills/review/SKILL.md',
         '/synthetic/projects/2.0.0/skills/review/SKILL.md', (1, 1, 1, 1))
    native = legacy.record('developer', legacy.block([('actual', plugin)]))
    native['payload']['content'][0]['type'] = 'output_text'
    result.append(('native-output-text', 'rollout', {'op': 'read', 'records': [native]},
                   {'names': ['actual'], 'surface': 'codex-tui'}))
    result.append(('native-precedes-legacy', 'rollout', {'op': 'read', 'records': [
        {'type': 'turn_context', 'payload': {'text': legacy.block([('legacy', plugin)])}}, native]},
        {'names': ['actual'], 'surface': 'codex-tui'}))
    return result


def grade(results):
    if len(results) != len(cases()):
        raise legacy.GradingError('incomplete Dojo observations')
    buckets = dict.fromkeys(('rollout', 'budget', 'mismatch'), True)
    checks = []
    for (name, bucket, _, expected), observed in zip(cases(), results, strict=True):
        passed = observed['ok'] and legacy.comparison(observed.get('value'), expected, oracle_version=5)
        buckets[bucket] &= passed
        checks.append({'id': name, 'bucket': bucket, 'pass': passed,
                       'expected': expected, 'observed': observed})
    return {'score': round(sum(buckets.values()) / len(buckets), 4),
            'buckets': buckets, 'checks': checks, 'oracle_version': 6}
