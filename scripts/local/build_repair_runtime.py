#!/usr/bin/env python3
"""Build a source/oracle-free immutable runtime for isolated repair suites."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='openbench-local/repair-runtime:codex-0.154.0')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='obench-runtime-build-') as d:
        context = Path(d)
        shutil.copyfile(ROOT / 'docker/repair-sandbox/Dockerfile', context / 'Dockerfile')
        (context / 'obench').mkdir()
        (context / 'obench/__init__.py').write_text('')
        shutil.copyfile(ROOT / 'obench/sandbox_gateway.py', context / 'obench/sandbox_gateway.py')
        # Campaign launchers intentionally use umask 077. These are public
        # runtime modules, and the confined UID must be able to import them.
        for path in context.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        hashes = {str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(context.rglob('*')) if p.is_file()}
        image_file = context.parent / (context.name + '.iid')
        try:
            subprocess.run(['docker', 'build', '--tag', args.tag, '--iidfile', str(image_file),
                            str(context)], check=True)
            identity = image_file.read_text().strip()
            result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                                     '--security-opt', 'no-new-privileges', '--user', '10001:10001',
                                     identity, 'codex', '--version'], check=True, capture_output=True, text=True)
            if result.stdout.strip() != 'codex-cli 0.154.0':
                raise RuntimeError('runtime CLI version differs from treatment')
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                            '--security-opt', 'no-new-privileges', '--user', '10001:10001',
                            identity, 'python3', '-m', 'obench.sandbox_gateway', '--help'],
                           check=True, capture_output=True, text=True)
            receipt = {'image_id': identity, 'tag': args.tag, 'context_sha256': hashes,
                       'codex_version': result.stdout.strip()}
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(receipt))
        finally:
            image_file.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
