#!/usr/bin/env python3
"""Every adapter must import in the flat /bench docker layout.

Inside ``--exec docker`` containers, adapters are not part of the ``obench``
package: entry.py file-loads them from ``/bench/adapters`` with only the
individually mounted modules alongside. Any relative import anywhere in that
chain dies with "attempted relative import with no known parent package".

This class of breakage shipped three separate times, each costing live cells:
router_spec (unmounted new dependency), candidates.py (``from .paths``), and
gateway_spec (``from . import gateway_profiles``, unmounted). Each was found
by a benchmark cell crashing, not by a test. This test reconstructs the exact
container layout from docker_exec's own mount list and imports every adapter
inside it, so the next drifted import fails here instead.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from obench import docker_exec

PKG = os.path.dirname(os.path.abspath(docker_exec.__file__))


def _mounted_module_paths():
    """The single-file modules docker_exec mounts next to /bench/entry.py."""
    paths = []
    for name in dir(docker_exec):
        if name.endswith("_PATH") and name not in ("ENTRY_PATH",):
            value = getattr(docker_exec, name)
            if isinstance(value, str) and value.endswith(".py") and os.path.isfile(value):
                paths.append(value)
    return paths


class FlatBenchLayoutTests(unittest.TestCase):
    def test_every_adapter_imports_in_the_container_layout(self):
        bench = tempfile.mkdtemp(prefix="flatbench-")
        try:
            os.makedirs(os.path.join(bench, "adapters"))
            for src in _mounted_module_paths():
                shutil.copy(src, os.path.join(bench, os.path.basename(src)))
            for src in glob.glob(os.path.join(PKG, "adapters", "*.py")):
                shutil.copy(src, os.path.join(bench, "adapters", os.path.basename(src)))

            # Import each adapter in a clean interpreter whose sys.path mirrors
            # entry.py's world: /bench and /bench/adapters, no obench package.
            probe = (
                "import glob, importlib.util, os, sys\n"
                f"bench = {bench!r}\n"
                "sys.path[:0] = [bench, os.path.join(bench, 'adapters')]\n"
                "failures = []\n"
                "for path in sorted(glob.glob(os.path.join(bench, 'adapters', '*.py'))):\n"
                "    name = os.path.splitext(os.path.basename(path))[0]\n"
                "    if name == '__init__':\n"
                "        continue\n"
                "    spec = importlib.util.spec_from_file_location(name, path)\n"
                "    mod = importlib.util.module_from_spec(spec)\n"
                "    try:\n"
                "        spec.loader.exec_module(mod)\n"
                "    except Exception as exc:\n"
                "        failures.append(f'{name}: {type(exc).__name__}: {exc}')\n"
                "print('\\n'.join(failures))\n"
                "sys.exit(1 if failures else 0)\n"
            )
            # -I (isolated) + cwd=bench: without this, the repo root rides in on
            # sys.path, `from obench import ...` succeeds via the real package,
            # and the flat-layout fallback is never exercised -- the negative
            # control (reverting the gateway_spec fix) passed this test until
            # isolation was added.
            proc = subprocess.run([sys.executable, "-I", "-c", probe],
                                  capture_output=True, text=True, timeout=120,
                                  cwd=bench)
            self.assertEqual(
                proc.returncode, 0,
                "adapters that cannot import in the flat /bench layout (each of "
                "these would crash every docker cell for its harness):\n"
                + proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(bench, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
