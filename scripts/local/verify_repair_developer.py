#!/usr/bin/env python3
"""Exercise each supported repair checkout through real Codex with fake inference."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
TASKS = ('dojo-evidence-pr60-c3-o5', 'am-benchmark-pr106-c2-o3')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for task in TASKS:
        subprocess.run([sys.executable, str(ROOT / 'scripts/local/verify_repair_codex.py'),
                        '--runtime-image', args.runtime_image, '--model', 'gpt-6.1-sol-high',
                        '--task', str(ROOT / 'benchmarks/harbor/local' / task),
                        '--output-dir', str(args.output / task)], check=True, cwd=ROOT)
    (args.output / 'receipt.json').write_text(json.dumps({
        'schema': 1, 'passed': True, 'tasks': list(TASKS), 'runtime_image': args.runtime_image,
        'live_inference': False}) + '\n')


if __name__ == '__main__':
    main()
