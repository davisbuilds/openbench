"""Versioned, closed registry; candidate metadata cannot load a host plugin."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib

from ..sandbox_grading import GradingError, task_manifest as legacy_task_manifest
from .. import repair_identity


@dataclass(frozen=True)
class Oracle:
    id: str
    module: str
    source_prefix: str
    protocol: str


ORACLES = {
    'activity-explorer-v2': Oracle('activity-explorer-v2',
        'obench.repair_oracles.activity_explorer_v2', 'web/', 'browser-observations-v1'),
    'activity-explorer-v1': Oracle('activity-explorer-v1',
        'obench.repair_oracles.activity_explorer', 'web/', 'browser-observations-v1'),
    'agentmonitor-benchmark-v3': Oracle(
        'agentmonitor-benchmark-v3',
        'obench.repair_oracles.agentmonitor_v3', 'src/', 'am-benchmark-observations-v1'),
    'agentmonitor-benchmark-v2': Oracle(
        'agentmonitor-benchmark-v2',
        'obench.repair_oracles.agentmonitor', 'src/', 'am-benchmark-observations-v1'),
}
MODULES = ('obench.sandbox_grading', 'obench.harbor_sandbox', 'obench.repair_worker', 'obench.repair_grading',
           'obench.repair_identity',
           'obench.repair_oracles.registry', 'obench.repair_oracles.agentmonitor',
           'obench.repair_oracles.agentmonitor_v3', 'obench.browser_worker',
           'obench.browser_policy', 'obench.repair_oracles.activity_explorer',
           'obench.browser_worker_v2', 'obench.repair_oracles.activity_explorer_v2')
PACKAGE = Path(__file__).resolve().parents[1]


def implementation_hashes():
    hashes = {name: hashlib.sha256((PACKAGE / (name.removeprefix('obench.').replace('.', '/') + '.py')).read_bytes()).hexdigest()
            for name in MODULES}
    hashes['obench/browser-seccomp.json'] = hashlib.sha256((PACKAGE / 'browser-seccomp.json').read_bytes()).hexdigest()
    return hashes


# Filled after all built-in modules exist; checked again before/after grading.
_LOADED_HASHES = implementation_hashes()


def select(metadata):
    key=metadata.get('openbench_oracle')
    if not isinstance(key,str) or key not in ORACLES:
        raise GradingError('unknown trusted repair oracle')
    oracle=ORACLES[key]
    try:
        repair_identity.resolve(metadata)
    except ValueError as exc:
        raise GradingError(str(exc)) from exc
    return oracle


def source_names(oracle, files):
    return {name for name in files if name.startswith(oracle.source_prefix)}


def task_manifest(root):
    if implementation_hashes() != _LOADED_HASHES:
        raise GradingError('trusted oracle implementation changed after import')
    manifest=legacy_task_manifest(Path(root))
    oracle=select(manifest['task_config'].get('metadata',{}))
    manifest.update(scheme=4, schema='openbench-isolated-repair-task-v2',
                    oracle={'id':oracle.id,'protocol':oracle.protocol},
                    implementation_sha256=_LOADED_HASHES)
    # Legacy protocol is not used, but its helpers remain an explicit dependency.
    manifest.pop('worker_entry_sha256')
    return manifest


def task_digest(root):
    return hashlib.sha256(json.dumps(task_manifest(root),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_task_binding(root, expected):
    if (not isinstance(expected,dict) or set(expected)!={'scheme','sha256'}
            or type(expected.get('scheme')) is not int or expected['scheme']!=4
            or not isinstance(expected.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',expected['sha256'])):
        raise GradingError('registered repair requires scheme 4 task binding')
    manifest=task_manifest(root)
    actual=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    on_disk=tomllib.loads((Path(root)/'task.toml').read_text()).get('metadata',{}).get('openbench_task_content_digest')
    if expected!=on_disk or actual!=expected['sha256']:
        raise GradingError('trusted task or oracle binding mismatch')
    return manifest
