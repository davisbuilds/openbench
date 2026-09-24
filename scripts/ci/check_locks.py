#!/usr/bin/env python3
"""Validate frozen CI dependency graphs without silently upgrading them."""
from pathlib import Path
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench.harbor_job import HARBOR_GIT_COMMIT


def main():
    requirements = ROOT / '.github/requirements'
    source = (requirements / 'harbor.in').read_text()
    if f'@{HARBOR_GIT_COMMIT}\n' not in source:
        raise SystemExit('Harbor CI input differs from the supported runtime commit')
    for name, version in [('build', '3.11'), ('harbor', '3.13'), ('harbor-build', '3.13')]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / f'{name}.txt'
            locked = requirements / f'{name}.txt'
            command = ['uv', 'pip', 'compile', '--universal', '--python-version', version,
                       '--no-header', '--no-annotate', '--quiet', '--constraint', str(locked),
                       str(requirements / f'{name}.in'), '--output-file', str(output)]
            if name in ('build', 'harbor-build'):
                command.append('--generate-hashes')
            subprocess.run(command, check=True)
            if output.read_bytes() != locked.read_bytes():
                raise SystemExit(f'CI lock drift: regenerate .github/requirements/{name}.txt')
    print('CI dependency locks match their inputs and the Harbor runtime pin')


if __name__ == '__main__':
    main()
