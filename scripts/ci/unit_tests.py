#!/usr/bin/env python3
"""Run the offline unit suite with one fresh interpreter per test module.

The serial reference remains ``python3 -m unittest discover -s obench/tests``;
``--workers 0`` (or ``OBENCH_TEST_WORKERS=0``) runs exactly that command.

Parallel mode discovers the suite the same way, then runs each module as
``python3 -m unittest discover -s obench/tests -p <module>.py`` from the
repository root, so imports, the working directory and the environment match
the serial run. A module never shares an interpreter with another module.
Every module must report exactly the tests that discovery found in it; a
missing, extra or unreported test fails the run.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
START = 'obench/tests'
ENV_WORKERS = 'OBENCH_TEST_WORKERS'
SERIAL = [sys.executable, '-m', 'unittest', 'discover', '-s', START]


def module_command(module, verbose=True):
    return [*SERIAL, '-p', f'{module}.py', *(['-v'] if verbose else [])]


def cpu_count():
    count = getattr(os, 'process_cpu_count', os.cpu_count)()
    return count or 1


def default_workers():
    value = os.environ.get(ENV_WORKERS, '').strip()
    if not value:
        return cpu_count()
    if not value.isdigit():
        raise SystemExit(f'{ENV_WORKERS} must be a non-negative integer, not {value!r}')
    return int(value)


def discover():
    """Expected test count per module, in discovery order, from the same loader."""
    sys.path.insert(0, str(ROOT))
    previous = os.getcwd()
    os.chdir(ROOT)
    try:
        suite = unittest.TestLoader().discover(START)
    finally:
        os.chdir(previous)
    counts = {}

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
            return
        if isinstance(item, unittest.loader._FailedTest):
            module = item._testMethodName  # an import failure carries the module name
        else:
            module = type(item).__module__
        counts[module] = counts.get(module, 0) + 1

    walk(suite)
    nested = sorted(module for module in counts if '.' in module)
    if nested:
        raise SystemExit('unit_tests.py runs top-level test modules only; '
                         f'extend it before adding test packages: {nested}')
    return counts


SUMMARY = re.compile(r'^Ran (\d+) tests? in ', re.M)
SKIPPED = re.compile(r'^(?:OK|FAILED) \(.*\bskipped=(\d+)', re.M)


# Modules measured at 9 seconds or more, slowest first (hosted CI and local,
# 2026-11). Order only: a stale or missing name changes the wall time, never
# which tests run. Refresh from the runner's "slowest modules" summary.
SLOW_FIRST = ('test_sandbox_gateway', 'test_proxy', 'test_matrix_queue_e2e', 'test_site',
              'test_evidence_identity', 'test_gateway_run', 'test_gateway_probe_http',
              'test_colima_benchmark_script', 'test_gateway_route', 'test_stall_kill',
              'test_gateway_proxy')


def schedule(counts):
    """Start known-slow modules first, then the rest by source size.

    A slow module that starts last sets the wall time; size is a weak proxy
    for the remainder.
    """
    rank = {module: index for index, module in enumerate(SLOW_FIRST)}
    return sorted(counts, key=lambda module: (rank.get(module, len(rank)),
                                              -(ROOT / START / f'{module}.py').stat().st_size))


def run_parallel(workers, verbose):
    started = time.monotonic()
    counts = discover()
    pending = schedule(counts)
    expected = sum(counts.values())
    print(f'unit tests: {expected} tests in {len(counts)} modules, {workers} workers, '
          f'{cpu_count()} CPUs', flush=True)
    actions = os.environ.get('GITHUB_ACTIONS') == 'true'
    running, results = {}, []
    with tempfile.TemporaryDirectory(prefix='obench-unit-tests-') as logs:
        try:
            while pending or running:
                while pending and len(running) < workers:
                    module = pending.pop(0)
                    log = open(Path(logs) / f'{module}.log', 'w+b')
                    process = subprocess.Popen(module_command(module), cwd=ROOT, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=subprocess.STDOUT)
                    running[module] = (process, log, time.monotonic())
                for module, (process, log, begun) in list(running.items()):
                    if process.poll() is None:
                        continue
                    del running[module]
                    log.seek(0)
                    output = log.read().decode(errors='replace')
                    log.close()
                    results.append(report(module, process.returncode, output, counts[module],
                                          time.monotonic() - begun, actions, verbose))
                time.sleep(0.02)
        finally:
            for process, log, _ in running.values():
                process.kill()
                process.wait()
                log.close()
    return summarize(results, expected, time.monotonic() - started)


def report(module, returncode, output, expected, elapsed, actions, verbose):
    ran = [int(value) for value in SUMMARY.findall(output)]
    skipped = [int(value) for value in SKIPPED.findall(output)]
    ran = ran[-1] if ran else None
    problem = None
    if returncode != 0:
        problem = f'exit {returncode}'
    elif ran != expected:
        problem = f'ran {ran} of {expected} discovered tests'
    status = 'FAIL' if problem else 'ok'
    line = f'{status:4} {module} ({ran if ran is not None else "?"} tests, {elapsed:.1f}s)'
    if problem:
        print(f'{line}: {problem}', flush=True)
        print(output, end='' if output.endswith('\n') else '\n', flush=True)
    elif actions:
        print(f'::group::{line}\n{output}::endgroup::', flush=True)
    else:
        print(line, flush=True)
        if verbose:
            print(output, end='', flush=True)
    return {'module': module, 'ran': ran or 0, 'skipped': skipped[-1] if skipped else 0,
            'elapsed': elapsed, 'problem': problem}


def summarize(results, expected, elapsed):
    failed = [result for result in results if result['problem']]
    ran = sum(result['ran'] for result in results)
    skipped = sum(result['skipped'] for result in results)
    print('\nslowest modules:')
    for result in sorted(results, key=lambda item: -item['elapsed'])[:10]:
        print(f"  {result['elapsed']:6.1f}s {result['module']}")
    print(f'\nRan {ran} of {expected} tests in {len(results)} modules in {elapsed:.1f}s '
          f'(skipped={skipped})')
    if failed:
        print(f'FAILED modules ({len(failed)}); reproduce one with:')
        for result in failed:
            print(f"  {' '.join(['python3', *module_command(result['module'])[1:]])}  # {result['problem']}")
        return 1
    if ran != expected:
        print('FAILED: test count differs from discovery')
        return 1
    print('OK')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--workers', type=int, default=None,
                        help=f'parallel module processes (default: ${ENV_WORKERS}, else CPU count); '
                             '0 runs the serial reference command')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='print every module log, not only failures (always grouped under GitHub Actions)')
    args = parser.parse_args(argv)
    workers = default_workers() if args.workers is None else args.workers
    if workers < 0:
        parser.error('--workers must be non-negative')
    if workers == 0:
        print(f"serial reference: {' '.join(['python3', *SERIAL[1:], '-v'])}", flush=True)
        return subprocess.run([*SERIAL, '-v'], cwd=ROOT).returncode
    return run_parallel(workers, args.verbose)


if __name__ == '__main__':
    sys.exit(main())
