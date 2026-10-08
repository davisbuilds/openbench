"""Actual-harness workflow evidence contract, independent of quality/report UI."""
import hashlib
import json
from pathlib import Path
import shlex

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_LOADED_SHA256 = sha(__file__)


def identity():
    if sha(__file__) != _LOADED_SHA256:
        raise ValueError('workflow implementation changed after import')
    return _LOADED_SHA256


def workflow_command(command):
    """Keep an AND-list failure from being hidden by later probe commands."""
    return 'bash --noprofile --norc -e -o pipefail -c ' + shlex.quote(command)


def validate_workflow(path, info, image, project_check):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError('workflow receipt must be a JSON object')
    required = {'status': 'passed', 'cleanup_confirmed': True, 'live_inference': False,
                'real_credentials': False, 'actual_tool_mutation': True,
                'tool_result_returned': True, 'final_response_present': True,
                'developer_workflows_passed': True, 'runtime_image': image,
                'task': info['task'], 'task_binding': info['task_binding'],
                'project_check': project_check,
                'probe_sha256': sha(ROOT / 'scripts/local/verify_repair_codex.py'),
                'workflow_sha256': identity()}
    if not project_check or any(type(value.get(k)) is not type(v) or value[k] != v for k, v in required.items()):
        raise ValueError('workflow evidence is incomplete, stale, or belongs to another task/image/command')
    required_files = {'agent/codex.txt', 'agent/developer-workflow.json', 'requests.jsonl', 'gateway.jsonl'}
    if info.get('revision', {}).get('oracle') == 'activity-explorer':
        if value.get('browser_image_received') is not True:
            raise ValueError('browser workflow lacks actual image transport')
        required_files.update(('agent/browser-before.png','agent/browser.png'))
    if set(value.get('evidence_sha256', {})) != required_files:
        raise ValueError('workflow receipt lacks bound execution evidence')
    for name, digest in value['evidence_sha256'].items():
        target = Path(path).parent / name
        if target.is_symlink() or sha(target) != digest:
            raise ValueError('workflow execution evidence changed')
    return value

