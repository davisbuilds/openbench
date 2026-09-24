"""Real tar/filesystem controls; Docker transport is replaced for offline CI."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from obench import harbor_sandbox as sandbox
from obench.tests.test_harbor_sandbox import archive
import tarfile


class LogExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        cls = sandbox._build_environment_class(object, None, None)
        self.env = cls.__new__(cls)
        self.env._containers = {"main": "offline-container"}
        # Export-content tests start after sealing; separate controls exercise
        # sealing refusal and the real Harbor/Docker lifecycle.
        self.env._sealed = True
        self.env._image_id = "sha256:" + "a" * 64
        self.env._gateway_ledger_sha = "b" * 64
        self.env.trial_paths = SimpleNamespace(verifier_dir=self.root / "verifier")

    async def test_sealing_failure_prevents_log_export(self):
        self.env.seal = AsyncMock(side_effect=sandbox.SandboxError("termination not confirmed"))
        target = self.root / "logs"
        with patch.object(sandbox, "docker_bytes", AsyncMock()) as transport:
            with self.assertRaisesRegex(sandbox.SandboxError, "termination not confirmed"):
                await self.env.download_dir("/logs/agent", target)
            transport.assert_not_awaited()
        self.assertFalse(target.exists())
        self.assertIn("log export failed", self.env._log_export_error)

    async def test_sealing_deadline_is_a_cleanup_error(self):
        import asyncio

        async def stalled_stop():
            await asyncio.sleep(60)

        self.env._seal_containers = stalled_stop
        with patch.object(sandbox, "SEAL_TIMEOUT_SECONDS", .01):
            with self.assertRaisesRegex(sandbox.SandboxError, "cleanup deadline"):
                await self.env.seal()

    async def test_large_log_exports_by_directory_and_file_but_not_as_source(self):
        content = b"x" * (sandbox.MAX_FILE_BYTES + 1)
        packed = sandbox.pack_files({"rollout.jsonl": content})
        with self.assertRaisesRegex(sandbox.SandboxError, "file exceeds"):
            sandbox.unpack_files(packed)
        with patch.object(sandbox, "docker_bytes", AsyncMock(return_value=packed)):
            await self.env.download_dir("/logs/agent", self.root / "logs")
            await self.env.download_file("/logs/agent/rollout.jsonl", self.root / "single/log.jsonl")
        self.assertEqual((self.root / "logs/rollout.jsonl").read_bytes(), content)
        self.assertEqual((self.root / "single/log.jsonl").read_bytes(), content)

    async def test_oversized_log_has_diagnostic_without_partial_export(self):
        content = b"private transcript" + b"x" * (16 * 1024 * 1024)
        packed = sandbox.pack_files({"rollout.jsonl": content})
        with patch.object(sandbox, "docker_bytes", AsyncMock(return_value=packed)):
            with self.assertRaises(sandbox.SandboxError):
                await self.env.download_dir("/logs/agent", self.root / "logs")
        self.assertFalse((self.root / "logs").exists())
        reports = list((self.root / "verifier/sandbox-exports").glob("*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["source"], "/logs/agent")
        self.assertEqual(report["archive_bytes"], len(packed))
        self.assertEqual(report["violation"], {"kind": "file_bytes", "path": "rollout.jsonl",
                         "observed": len(content), "limit": 16 * 1024 * 1024})
        self.assertNotIn("private transcript", reports[0].read_text())

    async def test_archive_attack_still_rejected_with_diagnostic(self):
        for name, kind, data in [("../escape", tarfile.REGTYPE, b"bad"),
                                 ("link", tarfile.SYMTYPE, b"/etc/passwd")]:
            with self.subTest(name=name):
                with patch.object(sandbox, "docker_bytes", AsyncMock(return_value=archive([(name, kind, data)]))):
                    with self.assertRaises(sandbox.SandboxError):
                        await self.env.download_dir("/logs/agent", self.root / "logs")
        self.assertFalse((self.root / "logs").exists())
        self.assertEqual(len(list((self.root / "verifier/sandbox-exports").glob("*.json"))), 2)

    async def test_diagnostic_persistence_failure_is_explicit(self):
        self.root.joinpath("verifier").write_text("not a directory")
        with patch.object(sandbox, "docker_bytes", AsyncMock(side_effect=sandbox.TransferLimitError("bounded"))):
            with self.assertRaisesRegex(sandbox.SandboxError, "diagnostic persistence failed"):
                await self.env.download_dir("/logs/agent", self.root / "logs")

    async def test_total_log_bound_accepts_exact_limit_and_refuses_one_extra_byte(self):
        # Smaller policy values keep the aggregate control cheap in offline CI.
        for size in (8, 9):
            packed = sandbox.pack_files({"first": b"a" * 8, "second": b"b" * size})
            target = self.root / str(size)
            with patch.object(sandbox, "MAX_LOG_FILE_BYTES", 10), patch.object(sandbox, "MAX_LOG_BYTES", 16):
                with patch.object(sandbox, "docker_bytes", AsyncMock(return_value=packed)):
                    if size == 8:
                        await self.env.download_dir("/logs/agent", target)
                        self.assertEqual((target / "second").read_bytes(), b"b" * 8)
                    else:
                        with self.assertRaisesRegex(sandbox.SandboxError, "content exceeds"):
                            await self.env.download_dir("/logs/agent", target)
                        self.assertFalse(target.exists())

    async def test_swallowed_export_failure_surfaces_at_stop_after_cleanup(self):
        self.env._deleted = False
        self.env._started = False
        self.env._cleanup = AsyncMock()
        with patch.object(sandbox, "docker_bytes", AsyncMock(side_effect=sandbox.TransferLimitError(
                "Docker output exceeded its bound", observed=65, limit=64))):
            with self.assertRaises(sandbox.TransferLimitError):
                await self.env.download_dir("/logs/agent", self.root / "logs")
        report = json.loads(next((self.root / "verifier/sandbox-exports").glob("*.json")).read_text())
        self.assertEqual(report["violation"], {"kind": "transfer_bytes", "observed_at_least": 65, "limit": 64})
        with self.assertRaisesRegex(sandbox.SandboxError, "log export failed"):
            await self.env.stop()
        self.env._cleanup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
