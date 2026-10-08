"""Independent human revisions for isolated repairs; byte seals remain authoritative.

Legacy aliases are permanent. New packages declare metadata.openbench_revision
and use <case>-c<case_revision>-o<oracle_revision> as their task name.
"""
import hashlib
import json
import re


FIELDS = {'case', 'case_revision', 'oracle', 'oracle_revision'}
LEGACY = {
    'dojo-evidence-pr60-v3': ('dojo-evidence-pr60', 1, 'dojo-evidence', 3),
    'dojo-evidence-pr60-v4': ('dojo-evidence-pr60', 2, 'dojo-evidence', 4),
    'dojo-evidence-pr60-v5': ('dojo-evidence-pr60', 2, 'dojo-evidence', 5),
    'am-benchmark-pr106-v3': ('am-benchmark-pr106', 1, 'agentmonitor-benchmark', 2),
    'am-benchmark-pr106-v4': ('am-benchmark-pr106', 1, 'agentmonitor-benchmark', 3),
}
CASE_ORACLES = {
    'activity-explorer': ('activity-explorer', (1,)),
    'dojo-evidence-pr60': ('dojo-evidence', (3, 4, 5, 6)),
    'am-benchmark-pr106': ('agentmonitor-benchmark', (2, 3)),
}


def _fields(values):
    return dict(zip(('case', 'case_revision', 'oracle', 'oracle_revision'), values))


def resolve(metadata):
    """Validate revision declarations before selecting any trusted oracle."""
    if not isinstance(metadata, dict):
        raise ValueError('invalid repair revision metadata')
    name = metadata.get('openbench_task')
    if not isinstance(name, str):
        raise ValueError('missing repair task identity')
    legacy = _fields(LEGACY[name]) if name in LEGACY else None
    revision = metadata.get('openbench_revision', legacy)
    if (not isinstance(revision, dict) or set(revision) != FIELDS
            or not isinstance(revision.get('case'), str)
            or not isinstance(revision.get('oracle'), str)
            or any(type(revision.get(key)) is not int or revision[key] < 1
                   for key in ('case_revision', 'oracle_revision'))):
        raise ValueError('invalid repair revision fields')
    allowed = CASE_ORACLES.get(revision['case'])
    if (allowed is None or revision['oracle'] != allowed[0]
            or revision['oracle_revision'] not in allowed[1]):
        raise ValueError('unsupported repair case/oracle revision')
    if legacy is not None:
        if revision != legacy:
            raise ValueError('legacy task alias has a different repair revision')
    elif name != task_name(revision):
        raise ValueError('repair task identity differs from declared revision')
    expected = (None if revision['oracle'] == 'dojo-evidence' else
                f"{revision['oracle']}-v{revision['oracle_revision']}")
    if metadata.get('openbench_oracle') != expected:
        raise ValueError('repair oracle selector differs from declared revision')
    return dict(revision)


def task_name(revision):
    return f"{revision['case']}-c{revision['case_revision']}-o{revision['oracle_revision']}"


def case_digest(files, file_modes, directory_modes):
    """Fingerprint the prompt and complete environment build context.

    Inputs are the already validated task manifest's file hashes and POSIX
    permissions. Directory entries bind empty directories too. Checkout-local
    timestamps and ownership are deliberately excluded. Oracle, provenance,
    package metadata and README edits are covered by the full task seal instead.
    """
    selected = {name: {'sha256': value, 'mode': file_modes[name]} for name, value in files.items()
                if name == 'instruction.md' or name.startswith('environment/')}
    if 'instruction.md' not in selected or not any(n.startswith('environment/') for n in selected):
        raise ValueError('repair case requires prompt and environment files')
    directories = {name: mode for name, mode in directory_modes.items()
                   if name == 'environment' or name.startswith('environment/')}
    value = {'schema': 1, 'files': selected, 'directories': directories}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def record(manifest):
    return {**resolve(manifest['task_config'].get('metadata', {})),
            'case_sha256': case_digest(manifest['task_files_sha256'],
                                       manifest['task_files_mode'],
                                       manifest['task_directories_mode'])}


def validate_record(value, name):
    """Validate sealed reports without reading or rewriting historical task trees."""
    if (not isinstance(value, dict) or set(value) != FIELDS | {'case_sha256'}
            or not isinstance(value.get('case_sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', value['case_sha256']) is None):
        raise ValueError('invalid repair revision record')
    revision = {key: value[key] for key in FIELDS}
    metadata = {'openbench_task': name, 'openbench_revision': revision}
    if revision['oracle'] != 'dojo-evidence':
        metadata['openbench_oracle'] = f"{revision['oracle']}-v{revision['oracle_revision']}"
    resolve(metadata)
