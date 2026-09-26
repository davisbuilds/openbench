#!/usr/bin/env python3
"""Develop AgentMonitor #123 controls in networkless workers; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench.harbor_sandbox import read_tree
from obench.repair_worker import run_worker
from obench.sandbox_grading import source_archive

CASE = ROOT/'experiments/repair_cases/am123'


def source(variant):
    manifest = json.loads((CASE/'SOURCE_MANIFEST.json').read_text())
    for relative, record in manifest['files'].items():
        if hashlib.sha256((CASE/relative).read_bytes()).hexdigest() != record['sha256']:
            raise RuntimeError('source snapshot drift: '+relative)
    files = read_tree(CASE/'source')
    if variant != 'buggy':
        files.update(read_tree(CASE/variant))
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--capture-fixture', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    program = (CASE/'driver.py').read_text()
    if args.capture_fixture:
        files = source('reference')
        results, worker = run_worker(args.runtime_image, source_archive(files), [{'kind': 'capture'}], program=program, timeout=60)
        result = results[0]['value']
        if result['worker']['exit_code'] != 0 or result['worker']['result']['error'] is not None:
            raise RuntimeError('reference schema capture failed')
        (args.output/'current.sql').write_text(result['ddl'])
        (args.output/'current-schema.json').write_text(json.dumps(result['state']['schema'], indent=2)+'\n')
        (args.output/'capture.json').write_text(json.dumps({'worker':worker,'result':result,'driver_sha256':hashlib.sha256(program.encode()).hexdigest()},indent=2)+'\n')
        print(json.dumps({'status':'captured','output':str(args.output),'runtime':worker}))
        return
    raise RuntimeError('control suite not yet connected')


if __name__ == '__main__':
    main()
