"""The offline probe must rebuild reviewed inputs before trusting an image."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/local/verify_dojo_container.py'


class ProbeImageTests(unittest.TestCase):
    def test_unattested_skip_build_is_rejected_before_docker_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'docker-was-accessed'
            docker = root / 'docker'
            docker.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                              + 'Path(' + repr(str(marker)) + ').touch()\nraise SystemExit(91)\n')
            docker.chmod(0o755)
            receipt = root / 'receipt.json'
            result = subprocess.run([sys.executable, str(SCRIPT), '--skip-build',
                                     '--image', 'untrusted-image:latest', '--receipt', str(receipt)],
                                    env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('unrecognized arguments: --skip-build', result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(receipt.exists())

    def test_build_resolves_its_immutable_output_instead_of_a_retargeted_tag(self):
        spec = importlib.util.spec_from_file_location("dojo_probe", SCRIPT)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        built = "sha256:" + "a" * 64
        resolved = "sha256:" + "b" * 64
        unrelated = "sha256:" + "c" * 64
        # A small Docker CLI fake keeps ordinary CI offline; the actual Docker
        # contaminated-image/rebuild control is exercised by the probe itself.
        backend = r"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == 'build':
    Path(args[args.index('--iidfile') + 1]).write_text(BUILT)
elif args[:2] == ['image', 'inspect']:
    print(RESOLVED if args[2] == BUILT else UNRELATED)
else:
    raise SystemExit('unexpected Docker operation')
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text("#!" + sys.executable + "\n" +
                              "BUILT=" + json.dumps(built) + "\n" +
                              "RESOLVED=" + json.dumps(resolved) + "\n" +
                              "UNRELATED=" + json.dumps(unrelated) + "\n" + backend)
            docker.chmod(0o755)
            with patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                self.assertEqual(probe.build_image("retargeted:latest"), resolved)
