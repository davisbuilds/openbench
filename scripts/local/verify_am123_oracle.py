#!/usr/bin/env python3
"""Develop AgentMonitor #123 controls in networkless workers; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench.harbor_sandbox import read_tree, write_files
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
    parser.add_argument('--variant', action='append', help='run only a named control variant')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    program = (CASE/'driver.py').read_text()
    if args.capture_fixture:
        files = source('reference')
        with tempfile.TemporaryDirectory(prefix='am123-source-') as staging:
            write_files(Path(staging), files)
            archive, hashes = source_archive(Path(staging), set(files))
        results, worker = run_worker(args.runtime_image, archive, [{'kind': 'capture'}], program=program, timeout=60)
        result = results[0]['value']
        if result['worker']['exit_code'] != 0 or result['worker']['result']['error'] is not None:
            raise RuntimeError('reference schema capture failed')
        (args.output/'current.sql').write_text(result['ddl'])
        (args.output/'current-schema.json').write_text(json.dumps(result['state']['schema'], indent=2)+'\n')
        (args.output/'capture.json').write_text(json.dumps({'worker':worker,'result':result,'driver_sha256':hashlib.sha256(program.encode()).hexdigest()},indent=2)+'\n')
        print(json.dumps({'status':'captured','output':str(args.output),'runtime':worker}))
        return
    from experiments.repair_cases.am123 import oracle
    fixture = json.loads((CASE/'FIXTURE_MANIFEST.json').read_text())
    if fixture['source_manifest_sha256'] != hashlib.sha256((CASE/'SOURCE_MANIFEST.json').read_bytes()).hexdigest():
        raise RuntimeError('fixture source identity changed')
    for relative, expected in fixture['files'].items():
        if hashlib.sha256((CASE/relative).read_bytes()).hexdigest() != expected:
            raise RuntimeError('fixture drift: '+relative)
    base = source('buggy')
    partial = source('partial')['src/db/schema.ts'].decode()
    reference = source('reference')['src/db/schema.ts'].decode()
    variants = oracle.variants(base['src/db/schema.ts'].decode(), partial, reference)
    expected_failures = {
        'buggy': {'structure','correction','legacy_read'},
        'historical-partial': {'structure','legacy_read'},
        'reference': set(), 'optimistic-retry': set(), 'sql-api-alternative': set(),
        'always-initialize': {'current_read'},
        'non-atomic-migration': {'correction','rollback'},
        'no-op-initializer': {'structure','foreign_keys','legacy_read'},
    }
    selected = args.variant or list(variants)
    if any(name not in variants for name in selected):
        raise ValueError('unknown control variant')
    summary = []
    for variant in selected:
        files = {**base, 'src/db/schema.ts': variants[variant].encode()}
        with tempfile.TemporaryDirectory(prefix='am123-source-') as staging:
            write_files(Path(staging), files)
            archive, hashes = source_archive(Path(staging), set(files))
        destination = args.output/variant
        destination.mkdir()
        buckets = {}
        for name, case in oracle.cases():
            results, worker = run_worker(args.runtime_image, archive, [case], program=program, timeout=240)
            value = results[0]['value']
            passed = oracle.grade(name, value)
            buckets[name] = passed
            record = {'case':name, 'variant':variant, 'passed':passed, 'worker':worker,
                      'source_sha256':hashes, 'observations':value,
                      'driver_sha256':hashlib.sha256(program.encode()).hexdigest(),
                      'oracle_sha256':hashlib.sha256((CASE/'oracle.py').read_bytes()).hexdigest(),
                      'fixture_manifest_sha256':hashlib.sha256((CASE/'FIXTURE_MANIFEST.json').read_bytes()).hexdigest()}
            (destination/(name+'.json')).write_text(json.dumps(record,indent=2)+'\n')
            gates = [r for r in value['observations'] if r.get('gate_observed')]
            print(json.dumps({'variant':variant,'case':name,'passed':passed,'gates':len(gates),
                              'completed_interleavings':sum(r.get('order')=='b_completed_before_a_release' for r in gates)}), flush=True)
            if variant == 'buggy' and name in ('structure','correction') and not any(
                    r.get('order') == 'b_completed_before_a_release' for r in gates):
                raise RuntimeError('baseline did not witness the intended interleaving')
        failed = {name for name, passed in buckets.items() if not passed}
        summary.append({'variant':variant,'buckets':buckets,'matches_expected':failed==expected_failures[variant]})
    result = {'status':'passed' if all(row['matches_expected'] for row in summary) else 'failed',
              'runtime_image':args.runtime_image,'controls':summary,'live_inference':False}
    (args.output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)
    if result['status'] != 'passed':
        raise RuntimeError('unexpected oracle control results; inspect preserved evidence')


if __name__ == '__main__':
    main()
