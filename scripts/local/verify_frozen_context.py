#!/usr/bin/env python3
"""Credential-free context discovery proof using only synthetic public guidance."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench.frozen_context import freeze_context
from obench.sandbox_grading import task_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'results'):
        raise ValueError('output must be under ignored results')
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'context'
    skill = source / 'codex/skills/offline-check'
    skill.mkdir(parents=True)
    (source / 'codex/AGENTS.md').write_text('Run available tests and inspect the diff. Offline guides are in /home/solver/context/resources.\n')
    (skill / 'SKILL.md').write_text('---\nname: offline-check\ndescription: Verify a local repair using the available tools.\n---\nRead /home/solver/context/resources/guide.md before checking.\n')
    resources = source / 'resources'; resources.mkdir()
    (resources / 'guide.md').write_text('Run the local helper and relevant tests.\n')
    helper = resources / 'check.sh'
    # Exceed exec_command's default initial wait to catch premature fake finals.
    helper.write_text('#!/bin/sh\nset -eu\ntest -r /app/AGENTS.md\nsleep 12\nprintf "context helper passed\\n"\n')
    helper.chmod(0o755)
    archive = output / 'context.tar'
    digest = freeze_context(source, archive)
    task = output / 'task'
    shutil.copytree(ROOT / 'benchmarks/harbor/local/dojo-evidence-pr60-v5', task)
    (task / 'environment/app/AGENTS.md').write_text('Project instructions: check Python syntax after editing.\n')
    executable = task / 'environment/app/workflow-check.sh'
    executable.write_text('#!/bin/sh\nset -eu\npython3 -m py_compile scripts/profiles/__init__.py\n')
    executable.chmod(0o755)
    config = task / 'task.toml'
    old = tomllib.loads(config.read_text())['metadata']['openbench_task_content_digest']['sha256']
    config.write_text(config.read_text().replace(old, task_digest(task)))
    subprocess.run([sys.executable, str(ROOT / 'scripts/local/verify_repair_codex.py'),
                    '--runtime-image', args.runtime_image, '--task', str(task),
                    '--model', 'gpt-6-luna-max', '--context-archive', str(archive),
                    '--context-sha256', digest, '--project-check',
                    './workflow-check.sh && /home/solver/context/resources/check.sh',
                    '--output-dir', str(output / 'probe')], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
