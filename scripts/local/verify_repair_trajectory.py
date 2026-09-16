#!/usr/bin/env python3
"""Replay Codex usage conversion without inference or changing source evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor.agents.installed.codex import Codex
from obench.atif import validate_trajectory
from obench.harbor_agents.sandbox_codex import CLI_VERSION, MODELS, SandboxCodex


def synthetic_events():
    def message(text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': text}]}}
    usage = {'input_tokens': 10, 'output_tokens': 5, 'cached_input_tokens': 2,
             'reasoning_output_tokens': 0, 'total_tokens': 15}
    def count(total):
        return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': total, 'last_token_usage': usage}}}
    return [{'type': 'session_meta', 'payload': {'id': 'synthetic-repeated-usage', 'cli_version': CLI_VERSION}},
            message('first'), count(usage), message('partial output'), count(usage),
            message('next'), count({k: v * 2 for k, v in usage.items()})]


def convert(cls, directory, model):
    agent = cls(logs_dir=directory, model_name=model, version=CLI_VERSION,
                reasoning_effort=MODELS[model])
    result = agent._convert_events_to_trajectory(directory)
    if result is None:
        raise RuntimeError('no trajectory produced')
    return result.model_dump(mode='json', exclude_none=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-dir', type=Path)
    parser.add_argument('--model', choices=MODELS, default='gpt-5.6-luna')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'results'):
        raise ValueError('replay evidence must stay under ignored results/')
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'rollout.jsonl').write_text('\n'.join(json.dumps(e) for e in synthetic_events()) + '\n')
        before = validate_trajectory(convert(Codex, root, args.model))
        assert any('expected sum 30, got 20' in error for error in before), before
        after = convert(SandboxCodex, root, args.model)
        assert not validate_trajectory(after), validate_trajectory(after)
    report = {'synthetic_stock_errors': before, 'synthetic_corrected_valid': True,
              'live_inference': False, 'scope': 'conversion replay, not a new model trial'}
    if args.session_dir:
        sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in args.session_dir.glob('*.jsonl')}
        assert len(sources) == 1, 'replay requires exactly one captured session'
        trajectory = convert(SandboxCodex, args.session_dir, args.model)
        errors = validate_trajectory(trajectory)
        assert not errors, errors
        (output / 'trajectory.json').write_text(json.dumps(trajectory, indent=2) + '\n')
        assert sources == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in args.session_dir.glob('*.jsonl')}
        report.update(source_sha256=sources, captured_replay_valid=True,
                      final_metrics=trajectory['final_metrics'])
    (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
