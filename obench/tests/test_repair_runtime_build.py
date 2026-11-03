"""Build context stays usable by the solver under a private launch umask."""
import hashlib
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
    print('codex-cli 0.162.0')
elif args[-3:] == ['bash', '-lc', 'python3 -m obench.repair_devtools check']:
    assert '--read-only' in args and '--tmpfs' in args
    print(json.dumps({'schema': 1, 'versions': {}, 'workflows': []}))
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
            self.assertEqual(set(modes), {
                'Dockerfile', 'node', 'node/package.json', 'node/package-lock.json',
                'tools', 'tools/package.json', 'tools/package-lock.json', 'obench',
                'obench/__init__.py', 'obench/sandbox_gateway.py', 'obench/repair_devtools.py',
                'obench/runtime_trust.py', 'trust', 'trust/blocked-root.pem',
                'python', 'python/requirements.txt'})
            self.assertEqual(modes['tools/package-lock.json'], 0o644)
            self.assertEqual(modes['obench'], 0o755)
            self.assertEqual(modes['obench/__init__.py'], 0o644)
            self.assertEqual(modes['obench/sandbox_gateway.py'], 0o644)
            self.assertEqual(modes['obench/repair_devtools.py'], 0o644)


class BrowserRuntimeBuildTests(unittest.TestCase):
    def test_fixed_browser_inputs_are_readable_and_bound_into_the_receipt(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            docker = temp / 'docker'
            docker.write_text('#!/usr/bin/env python3\n' + '''
import hashlib, json, pathlib, stat, sys
args = sys.argv[1:]
if args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'sha256:'+'a'*64, 'Config': {
        'Labels': {'org.openbench.developer-environment': '4'}}}]))
elif args[0] == 'build':
    context = pathlib.Path(args[-1])
    files = {p.name: {'mode': stat.S_IMODE(p.stat().st_mode),
                     'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in context.iterdir()}
    (pathlib.Path(__file__).parent / 'context.json').write_text(
        json.dumps({'files': files, 'tag': args[args.index('--tag') + 1]}))
    pathlib.Path(args[args.index('--iidfile')+1]).write_text('sha256:'+'b'*64)
elif args[0] == 'tag' or args[:2] == ['image', 'rm']:
    pass
else:
    raise AssertionError(args)
''')
            docker.chmod(0o755)
            receipt = temp / 'receipt.json'
            result = subprocess.run(
                [sys.executable, str(root / 'scripts/local/build_browser_runtime.py'),
                 '--base-image', 'sha256:' + 'a' * 64, '--receipt', str(receipt)],
                env=dict(os.environ, PATH=str(temp) + os.pathsep + os.environ['PATH']),
                umask=0o077, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads((temp / 'context.json').read_text())
            names = {'Dockerfile', 'package.json', 'package-lock.json',
                     'fixed-browser-lock.json', 'install_fixed_browser.py', 'sandbox_gateway.py'}
            self.assertEqual(set(context['files']), names)
            self.assertEqual(context['tag'],
                             'openbench-local/browser-runtime:chrome-156.0.8078.80-v2')
            built = json.loads(receipt.read_text())
            self.assertEqual(set(built['inputs']), names)
            for name, recorded in context['files'].items():
                source = (root / 'obench/sandbox_gateway.py' if name == 'sandbox_gateway.py'
                          else root / 'docker/browser-runtime' / name)
                expected = hashlib.sha256(source.read_bytes()).hexdigest()
                self.assertEqual(recorded['mode'], 0o644, name)
                self.assertEqual(recorded['sha256'], expected, name)
                self.assertEqual(built['inputs'][name], expected, name)
            self.assertEqual(built['browser_source_lock'], json.loads(
                (root / 'docker/browser-runtime/fixed-browser-lock.json').read_text()))
            self.assertIn('qualification required', built['status'])

    def test_supported_base_revisions_build_and_future_revision_is_rejected(self):
        root = Path(__file__).resolve().parents[2]
        for revision in ('3', '4', '5'):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                docker = temp/'docker'
                docker.write_text('#!/usr/bin/env python3\n'+'''
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'sha256:'+'a'*64, 'Config': {
        'Labels': {'org.openbench.developer-environment': os.environ['TEST_BASE_REVISION']}}}]))
elif args[0] == 'build':
    pathlib.Path(args[args.index('--iidfile')+1]).write_text('sha256:'+'b'*64)
elif args[0] == 'tag' or args[:2] == ['image', 'rm']:
    pass
else:
    raise AssertionError(args)
''')
                docker.chmod(0o755)
                receipt = temp/'receipt.json'
                result = subprocess.run([sys.executable,
                    str(root/'scripts/local/build_browser_runtime.py'),
                    '--base-image', 'sha256:'+'a'*64, '--receipt', str(receipt)],
                    env=dict(os.environ, PATH=str(temp)+os.pathsep+os.environ['PATH'],
                             TEST_BASE_REVISION=revision), capture_output=True, text=True)
                if revision == '5':
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(receipt.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(receipt.read_text())['base_image_id'], 'sha256:'+'a'*64)
