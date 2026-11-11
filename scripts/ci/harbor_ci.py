#!/usr/bin/env python3
"""Select Harbor lanes and validate the aggregate result."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_quality_inputs as inputs


def classify(repo, event, base=None, head=None, merged=None):
    report = {'event': event, 'lanes': list(inputs.LANES)}
    if event not in ('pull_request', 'push'):
        return dict(report, mode='forced', reason='manual or unclassified event')
    try:
        decisions = {lane: inputs.decide(repo, base, head,
                                         merged if event == 'pull_request' else None, lane=lane,
                                         comparison=event) for lane in inputs.LANES}
        if any(type(item.get('run')) is not bool for item in decisions.values()):
            raise ValueError('classifier did not return boolean decisions')
        return dict(report, mode='classified', decisions=decisions,
                    lanes=[lane for lane in inputs.LANES if decisions[lane]['run']])
    except Exception as error:
        # An incomplete comparison never becomes a partial or empty matrix.
        return dict(report, mode='fallback', reason=f'{type(error).__name__}: {error}')


def validate_gate(report, event, classify_result, integration_result):
    if classify_result != 'success':
        raise ValueError('classification did not succeed')
    if not isinstance(report, dict) or report.get('event') != event:
        raise ValueError('missing or mismatched classification event')
    lanes = report.get('lanes')
    if not isinstance(lanes, list) or lanes != [lane for lane in inputs.LANES if lane in lanes]:
        raise ValueError('invalid selected lanes')
    mode = report.get('mode')
    if mode == 'classified':
        decisions = report.get('decisions')
        if event not in ('pull_request', 'push') or not isinstance(decisions, dict):
            raise ValueError('invalid classified event or decisions')
        if set(decisions) != set(inputs.LANES):
            raise ValueError('incomplete lane decisions')
        for lane, decision in decisions.items():
            if (not isinstance(decision, dict) or type(decision.get('run')) is not bool
                    or decision['run'] != (lane in lanes)):
                raise ValueError('lane decision contradicts selected matrix')
    elif mode in ('fallback', 'forced'):
        if lanes != list(inputs.LANES):
            raise ValueError('fallback and forced runs require every lane')
    else:
        raise ValueError('unknown classification mode')
    expected = 'success' if lanes else 'skipped'
    if integration_result != expected:
        raise ValueError(f'integration result {integration_result!r}; expected {expected!r}')


def output_decision(report, output, summary):
    # Keep the gate payload bounded; detailed path evidence stays in the log.
    compact = {key: report[key] for key in ('event', 'mode', 'lanes')}
    if report['mode'] == 'classified':
        compact['decisions'] = {lane: {'run': item['run']}
                                for lane, item in report['decisions'].items()}
    if output:
        with open(output, 'a') as stream:
            stream.write('decision=' + json.dumps(compact, separators=(',', ':')) + '\n')
            stream.write('run=' + ('true' if report['lanes'] else 'false') + '\n')
            # GitHub rejects an empty matrix even on some skipped-job paths.
            stream.write('matrix=' + json.dumps(report['lanes'] or ['not-selected']) + '\n')
    if summary:
        with open(summary, 'a') as stream:
            stream.write('### Harbor lane selection\n\n')
            stream.write('Mode: ' + report['mode'] + '\n\n')
            for lane in inputs.LANES:
                stream.write('- ' + lane + ': ' + ('run' if lane in report['lanes'] else 'skip') + '\n')
            stream.write('\nFull comparison evidence is in the classify job log.\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    select = commands.add_parser('classify')
    select.add_argument('--repo', default='.')
    select.add_argument('--event', required=True)
    select.add_argument('--base')
    select.add_argument('--head')
    select.add_argument('--merged')
    select.add_argument('--github-output', default=os.environ.get('GITHUB_OUTPUT'))
    select.add_argument('--summary', default=os.environ.get('GITHUB_STEP_SUMMARY'))
    gate = commands.add_parser('gate')
    gate.add_argument('--event', required=True)
    gate.add_argument('--classify-result', required=True)
    gate.add_argument('--integration-result', required=True)
    gate.add_argument('--decision', required=True)
    args = parser.parse_args(argv)
    if args.command == 'classify':
        report = classify(args.repo, args.event, args.base, args.head, args.merged)
        print(json.dumps(report, sort_keys=True))
        output_decision(report, args.github_output, args.summary)
        return 0
    try:
        report = json.loads(args.decision)
        validate_gate(report, args.event, args.classify_result, args.integration_result)
    except (ValueError, TypeError) as error:
        print(f'Harbor gate failed: {error}', file=sys.stderr)
        return 1
    print('Every selected lane passed.' if report['lanes'] else
          'No integration inputs changed; all lanes deliberately skipped.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
