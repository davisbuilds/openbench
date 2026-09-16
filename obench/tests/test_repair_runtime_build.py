"""Build context stays usable by the solver under a private launch umask."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RepairRuntimeBuildTests(unittest.TestCase):
    def test_private_umask_keeps_runtime_modules_readable_by_solver(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            docker = temp / 'docker'
            docker.write_text("#!/usr/bin/env python3\n" + """
import json, pathlib, stat, sys
args = sys.argv[1:]
if args[0] == 'build':
    context = pathlib.Path(args[-1])
    modes = {str(p.relative_to(context)): stat.S_IMODE(p.stat().st_mode)
             for p in context.rglob('*')}
    (pathlib.Path(__file__).parent / 'modes.json').write_text(json.dumps(modes))
    pathlib.Path(args[args.index('--iidfile') + 1]).write_text('sha256:' + 'a' * 64)
elif args[-2:] == ['codex', '--version']:
    print('codex-cli 0.154.0')
else:
    assert 'obench.sandbox_gateway' in args and '--help' in args
    print('gateway help')
""")
            docker.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(root / 'scripts/local/build_repair_runtime.py'),
                 '--receipt', str(temp / 'receipt.json')],
                env=dict(os.environ, PATH=str(temp) + os.pathsep + os.environ['PATH']),
                umask=0o077, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            modes = json.loads((temp / 'modes.json').read_text())
            self.assertEqual(modes['obench'], 0o755)
            self.assertEqual(modes['obench/__init__.py'], 0o644)
            self.assertEqual(modes['obench/sandbox_gateway.py'], 0o644)
