"""Operator-authorized evaluation packages on the canonical repair path.

Task metadata describes identity only. An explicit Pin is the authority to use
host evaluator code; its path and trust flag come from operator configuration.
"""
from dataclasses import dataclass
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tomllib

from . import evaluation_package as packages, repair_identity
from .harbor_sandbox import read_tree, write_files
from .sandbox_grading import GradingError, freeze_submission, task_manifest as tree_manifest


_LOADED_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Pin:
    archive: Path
    sha256: str
    trust_evaluator: bool

    def load(self):
        if self.trust_evaluator is not True:
            raise GradingError('private campaign requires explicit evaluator trust')
        return packages.load(self.archive, self.sha256)

    def kwargs(self):
        self.load()
        return {'evaluation_archive': str(self.archive), 'evaluation_sha256': self.sha256,
                'trust_evaluator': True}


def pin(archive=None, sha256=None, trust_evaluator=False):
    if archive is None and sha256 is None and trust_evaluator is False:
        return None
    if archive is None or sha256 is None or trust_evaluator is not True:
        raise GradingError('evaluation archive, SHA256 and explicit evaluator trust are required together')
    value = Pin(Path(archive).absolute(), sha256, trust_evaluator)
    value.load()
    return value


def metadata(package):
    d = package.descriptor
    return {'sha256': package.archive.sha256, 'evaluation': d}


def revision(package):
    d = package.descriptor
    return {'case': d['id'], 'case_revision': d['case_revision'],
            'oracle': 'private-evaluation', 'oracle_revision': d['oracle_revision']}


def implementation():
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != _LOADED_SHA256:
        raise GradingError('package campaign implementation changed after import')
    return {**packages.implementation(), 'obench.package_campaign':
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def task_manifest(root, authorization):
    if authorization is None:
        raise GradingError('private task requires an operator evaluation pin')
    package = authorization.load()
    manifest = tree_manifest(Path(root))
    meta = manifest['task_config']['metadata']
    if (meta.get('openbench_evaluation') != metadata(package)
            or meta.get('openbench_revision') != revision(package)
            or meta.get('openbench_oracle') is not None):
        raise GradingError('task differs from operator evaluation pin')
    # The full project and prompt must be the approved solver projection.
    expected = {'instruction.md': package.archive.files['solver/instruction.md']}
    expected.update({'environment/app/' + n.removeprefix('solver/workspace/'): b
                    for n, b in package.archive.files.items() if n.startswith('solver/workspace/')})
    actual = {n: h for n, h in manifest['task_files_sha256'].items()
              if n == 'instruction.md' or n.startswith('environment/app/')}
    approved_modes = {}
    for name in expected:
        source = 'solver/instruction.md' if name == 'instruction.md' else 'solver/workspace/' + name.removeprefix('environment/app/')
        approved_modes[name] = package.archive.manifest['files'][source]['mode']
    actual_modes = {name: manifest['task_files_mode'][name] for name in actual}
    if (actual != {n: hashlib.sha256(b).hexdigest() for n, b in expected.items()}
            or actual_modes != approved_modes):
        raise GradingError('task solver projection differs from approved package')
    manifest.update(scheme=5, schema='openbench-private-evaluation-task-v1',
                    evaluation_package=metadata(package), implementation_sha256=implementation())
    manifest.pop('worker_entry_sha256')
    return manifest


def digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_task_binding(root, expected, authorization):
    manifest = task_manifest(root, authorization)
    actual = {'scheme': 5, 'sha256': digest(manifest)}
    on_disk = tomllib.loads((Path(root) / 'task.toml').read_text())['metadata'].get('openbench_task_content_digest')
    if not isinstance(expected, dict) or expected != actual or on_disk != actual or type(expected.get('scheme')) is not int:
        raise GradingError('private task or evaluator binding mismatch')
    return manifest


def export_task(package, destination):
    """Create one Harbor task without executing evaluator code or exporting it."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    rev = revision(package)
    name = repair_identity.task_name(rev)
    files = {'instruction.md': package.archive.files['solver/instruction.md'],
             'environment/Dockerfile': b'FROM scratch\n',
             'tests/test.sh': b'#!/bin/sh\n# Host-owned verifier required.\nexit 2\n'}
    files.update({'environment/app/' + n.removeprefix('solver/workspace/'): b
                  for n, b in package.archive.files.items() if n.startswith('solver/workspace/')})
    write_files(destination, files)
    for n in files:
        src = 'solver/instruction.md' if n == 'instruction.md' else 'solver/workspace/' + n.removeprefix('environment/app/')
        mode = package.archive.manifest['files'].get(src, {}).get('mode', 0o644)
        (destination / n).chmod(mode)
    q = json.dumps
    d = package.descriptor
    text = f'''schema_version = "1.4"
artifacts = [{{ source = "/app", destination = "workspace" }}]
[task]
name = {q('openbench/' + name)}
version = "1.0.0"
description = "Operator-owned evaluation task"
authors = []
keywords = ["openbench"]
[metadata]
openbench_task = {q(name)}
openbench_workspace_kind = "snapshot"
[metadata.openbench_revision]
case = {q(rev['case'])}
case_revision = {rev['case_revision']}
oracle = "private-evaluation"
oracle_revision = {rev['oracle_revision']}
[metadata.openbench_evaluation]
sha256 = {q(package.archive.sha256)}
[metadata.openbench_evaluation.evaluation]
schema = 1
id = {q(d['id'])}
case_revision = {d['case_revision']}
oracle_revision = {d['oracle_revision']}
split = {q(d['split'])}
backend = {q(d['backend'])}
[metadata.openbench_harbor_export]
schema_version = 1
base_image = "operator-pinned-runtime"
network_mode = "no-network"
[verifier]
timeout_sec = 120.0
[agent]
timeout_sec = 1800.0
[environment]
network_mode = "no-network"
os = "linux"
cpus = 2
memory_mb = 2048
storage_mb = 10240
gpus = 0
'''
    config = destination / 'task.toml'
    config.write_text(text)
    # Export is non-executing. A captured package supplies validation without
    # treating task metadata as a pin or approving host execution.
    class Captured:
        def load(self):
            return package
    seal = digest(task_manifest(destination, Captured()))
    config.write_text(text + f'\n[metadata.openbench_task_content_digest]\nscheme = 5\nsha256 = "{seal}"\n')
    return {'task': str(destination), 'task_binding': {'scheme': 5, 'sha256': seal}}


def grade_submission(root, authorization, image, *, timeout=90):
    result = packages.replay(authorization.archive, authorization.sha256, root, image,
                             trust_evaluator=authorization.trust_evaluator, timeout=timeout)
    worker = result['worker']
    if worker is not None:
        worker = {**worker, 'runtime_dependencies': result['runtime_dependencies']}
    return {**result, 'worker': worker, 'oracle_id': 'private-evaluation',
            'protocol': 'browser-observations-v1',
            'evaluation_package': metadata(authorization.load())}


class _PackageVerifier:
    def __init__(self, *args, worker_image=None, worker_timeout=90,
                 evaluation_archive=None, evaluation_sha256=None, trust_evaluator=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorization = pin(evaluation_archive, evaluation_sha256, trust_evaluator)
        if self.authorization is None or not worker_image:
            raise GradingError('private verifier requires operator pin and worker image')
        self.worker_image, self.worker_timeout = worker_image, worker_timeout

    async def verify(self):
        from harbor.models.verifier.result import VerifierResult
        start = time.monotonic()
        meta = self.task.config.metadata
        root = self.task.paths.task_dir
        expected = meta.get('openbench_task_content_digest')
        binding = validate_task_binding(root, expected, self.authorization)
        with tempfile.TemporaryDirectory(prefix='obench-package-submission-') as tmp:
            frozen, rejected = await freeze_submission(self.environment, Path(tmp))
            validate_task_binding(root, expected, self.authorization)
            if rejected is None:
                graded = await asyncio.to_thread(grade_submission, tmp, self.authorization,
                                                self.worker_image, timeout=self.worker_timeout)
            else:
                graded = {'score': 0, 'candidate_failure': 'invalid_source_artifact',
                          'source_sha256': None, 'worker': None, 'oracle_id': 'private-evaluation',
                          'protocol': 'browser-observations-v1',
                          'evaluation_package': metadata(self.authorization.load())}
        validate_task_binding(root, expected, self.authorization)
        score = graded['score']
        logs = self.trial_paths.verifier_dir
        logs.mkdir(parents=True, exist_ok=True)
        evidence = {'schema_version': 'openbench-verifier-evidence-v2',
                    'openbench_task_content_digest': expected,
                    'openbench_harbor_export': meta['openbench_harbor_export'],
                    'checker_exit': 0 if score == 1 else 1, 'parsed_score': score, 'reward': score,
                    'verifier_duration_seconds': time.monotonic() - start}
        (logs / 'reward.txt').write_text(str(score) + '\n')
        (logs / 'openbench-verifier-evidence.json').write_text(json.dumps(evidence) + '\n')
        (logs / 'sandbox-grading.json').write_text(json.dumps(
            {'freeze': frozen, 'grading': graded, 'task_binding': binding}) + '\n')
        return VerifierResult(rewards={'reward': score})


def __getattr__(name):
    if name == 'PackageVerifier':
        from harbor.verifier.base import BaseVerifier
        cls = type(name, (_PackageVerifier, BaseVerifier), {'__module__': __name__})
        globals()[name] = cls
        return cls
    raise AttributeError(name)
