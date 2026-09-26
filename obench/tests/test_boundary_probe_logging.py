"""Concurrent canary observations must remain complete JSONL records."""
import ast
import functools
import io
import json
from pathlib import Path
import threading
import time
import unittest


class BoundaryProbeLoggingTests(unittest.TestCase):
    def test_socket_handlers_cannot_interleave_record_and_newline(self):
        # Read the actual container program without importing optional Harbor.
        source = Path(__file__).resolve().parents[2] / 'scripts/local/verify_repair_sandbox.py'
        module = ast.parse(source.read_text())
        server = next(ast.literal_eval(node.value) for node in module.body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == 'SERVER'
                          for target in node.targets))
        # Load its definitions without launching privileged-port listeners.
        tree = ast.parse(server)
        definitions = ast.Module(body=[node for node in tree.body if isinstance(
            node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))], type_ignores=[])

        class YieldingOutput(io.StringIO):
            def write(self, data):
                result = super().write(data)
                # Force the scheduling window between print's separate writes.
                time.sleep(.001)
                return result

        output = YieldingOutput()
        namespace = {'print': functools.partial(print, file=output)}
        exec(compile(definitions, '<canary-server>', 'exec'), namespace)
        start = threading.Barrier(6)

        def handler(index):
            start.wait()
            for sequence in range(20):
                namespace['emit'](json.dumps({'handler': index, 'sequence': sequence}))

        threads = [threading.Thread(target=handler, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'canary logging stalled')
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(sorted((row['handler'], row['sequence']) for row in rows),
                         [(index, sequence) for index in range(6) for sequence in range(20)])
