#!/usr/bin/env python3
"""Offline package projection/host-read/frozen-replay control; no inference calls.

Requires a development package with controls/reference mirroring its web subtree.
This proves a container route, not authenticated harness or campaign admission.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from obench import evaluation_package as package
from obench.repair_evidence import Operation


async def verify(archive, sha256, image, output, *, trust_evaluator=False):
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths
    from obench.harbor_sandbox import RepairSandbox

    if not trust_evaluator:
        raise ValueError('explicit evaluator trust required')
    approved = package.load(archive, sha256)
    if approved.descriptor['split'] != 'development':
        raise ValueError('qualification controls must be development material')
    reference = {n.removeprefix('controls/reference/'): b for n, b in approved.archive.files.items()
                 if n.startswith('controls/reference/')}
    with Operation(output, 'package-boundary-control') as evidence:
        with tempfile.TemporaryDirectory(prefix='obench-package-control-') as temporary:
            root = Path(temporary).resolve()
            package.export(approved, root / 'export')
            source = root / 'export/workspace'
            sources = {p.relative_to(source).as_posix() for p in (source / 'web').rglob('*') if p.is_file()}
            if not reference or set(reference) != sources:
                raise ValueError('reference must mirror the solver web subtree for this control')
            environment = root / 'environment'
            shutil.copytree(source, environment / 'app')
            # A positive host read of the exact forbidden target precedes denial.
            if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
                raise ValueError('host archive detector failed')
            reports = []
            for mode in ('baseline', 'reference'):
                with evidence.stage(mode):
                    env = RepairSandbox(environment_dir=environment, environment_name='package-control',
                        session_id='package-' + uuid.uuid4().hex, trial_paths=TrialPaths(root / mode),
                        task_env_config=EnvironmentConfig(network_mode='no-network', cpus=1, memory_mb=512),
                        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
                        runtime_image=image, source_paths=sorted(sources))
                    try:
                        await env.start()
                        probe = r'''
import pathlib,subprocess,sys
root=pathlib.Path('/app'); allowed=root/'positive-control'
allowed.write_text('allowed')
assert subprocess.check_output([sys.executable,'-c','from pathlib import Path; print(Path("/app/positive-control").read_text())'],text=True).strip()=='allowed'
allowed.unlink()
target=pathlib.Path(sys.argv[1]); link=pathlib.Path('/tmp/package-link'); link.symlink_to(target)
for path in (target,link):
    try: path.read_bytes()
    except OSError: pass
    else: raise AssertionError('private host archive readable')
    read=subprocess.run([sys.executable,'-c','from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()',str(path)],capture_output=True)
    assert read.returncode != 0
link.unlink()
assert not (root/'evaluator.py').exists()
assert not (root/'cases.json').exists()
assert not (root/'fixtures').exists()
assert not (root/'controls').exists()
print('workspace-positive-host-denied')
'''
                        result = await env.exec('python3 -c ' + shlex.quote(probe) + ' ' + shlex.quote(str(archive)))
                        if result.return_code or result.stdout.strip() != 'workspace-positive-host-denied':
                            raise RuntimeError('package boundary control failed')
                        if mode == 'reference':
                            for name, data in reference.items():
                                local = root / 'overlay' / name
                                local.parent.mkdir(parents=True, exist_ok=True); local.write_bytes(data)
                                staged = '/tmp/reference-' + uuid.uuid4().hex
                                await env.upload_file(local, staged)
                                copied = await env.exec('cp ' + shlex.quote(staged) + ' ' + shlex.quote('/app/' + name))
                                if copied.return_code:
                                    raise RuntimeError('reference overlay failed')
                        frozen = root / ('frozen-' + mode)
                        receipt = await env.freeze_source(frozen)
                        if not receipt['solver_stopped'] or not receipt['broker_revoked']:
                            raise RuntimeError('solver boundary is not sealed')
                        report = await asyncio.to_thread(package.replay, archive, sha256, frozen, image,
                                                         trust_evaluator=True)
                        evidence.artifact('replay', {'mode': mode, 'freeze': receipt, 'result': report})
                        if report['solved'] != (mode == 'reference'):
                            raise RuntimeError('baseline/reference polarity failed')
                        reports.append({'mode': mode, 'score': report['score'],
                                        'workspace_positive': True, 'host_archive_denied': True,
                                        'direct_symlink_subprocess': True, 'solver_stopped': True})
                    finally:
                        await env.stop()
        result = {'schema': 1, 'status': 'passed', 'package_sha256': sha256, 'controls': reports,
                  'model_calls': 0, 'campaign_eligible': False,
                  'scope': 'offline container/package boundary control; actual harness admission remains required'}
        evidence.finish(result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--trust-evaluator', action='store_true')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.archive.absolute(), args.sha256, args.image, args.output,
                                      trust_evaluator=args.trust_evaluator)), sort_keys=True))


if __name__ == '__main__':
    main()
