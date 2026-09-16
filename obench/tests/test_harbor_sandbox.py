"""Offline admission controls; importing these tests needs no Harbor package."""
import copy
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from obench import harbor_sandbox as sandbox


IMAGE = "sha256:" + "a" * 64
TOKEN = "b" * 24


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as handle:
        for name, kind, data in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = data.decode()
            elif kind == tarfile.REGTYPE:
                entry.size = len(data)
            handle.addfile(entry, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


class SourceBoundaryTests(unittest.TestCase):
    def test_regular_sources_round_trip_and_keep_exact_bytes(self):
        files = {"scripts/profiles/a.py": b"print('yes')\n", "requirements.txt": b"PyYAML==6.0.3\n"}
        packed = sandbox.pack_files(files)
        self.assertEqual(sandbox.unpack_files(packed, allowed=set(files)), files)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox.write_files(root, files)
            self.assertEqual(sandbox.read_tree(root), files)
            self.assertEqual(sandbox.source_receipt(files), sandbox.source_receipt(sandbox.read_tree(root)))

    def test_archive_rejects_links_special_files_and_path_escapes(self):
        for name, kind, data in (("../escape", tarfile.REGTYPE, b"bad"),
                                 ("/absolute", tarfile.REGTYPE, b"bad"),
                                 ("x", tarfile.SYMTYPE, b"/secret"),
                                 ("x", tarfile.LNKTYPE, b"safe"),
                                 ("pipe", tarfile.FIFOTYPE, b""),
                                 ("device", tarfile.CHRTYPE, b"")):
            with self.subTest(name=name, kind=kind), self.assertRaises(sandbox.SandboxError):
                sandbox.unpack_files(archive([(name, kind, data)]))

    def test_duplicate_paths_and_removed_required_source_fail(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.unpack_files(archive([("a", tarfile.REGTYPE, b"first"), ("a", tarfile.REGTYPE, b"last")]))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.unpack_files(sandbox.pack_files({"a": b"x"}), allowed={"a", "missing"})

    def test_unapproved_regular_files_are_not_exported_but_links_still_fail(self):
        data = sandbox.pack_files({"source.py": b"safe", "notes.txt": b"ignored"})
        self.assertEqual(sandbox.unpack_files(data, allowed={"source.py"}), {"source.py": b"safe"})
        with self.assertRaises(sandbox.SandboxError):
            sandbox.unpack_files(archive([("source.py", tarfile.REGTYPE, b"safe"),
                                          ("ignored", tarfile.SYMTYPE, b"/secret")]), allowed={"source.py"})

    def test_byte_limits_fail_before_writing(self):
        data = sandbox.pack_files({"one": b"1234", "two": b"1234"})
        with self.assertRaises(sandbox.SandboxError):
            sandbox.unpack_files(data, total_limit=7)

    def test_real_filesystem_links_are_rejected_without_changing_canary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canary = root / "canary"
            canary.write_bytes(b"unchanged")
            workspace = root / "workspace"
            workspace.mkdir()
            link = workspace / "link"
            link.symlink_to(canary)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.read_tree(workspace)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.write_files(workspace, {"link": b"bad"})
            link.unlink()
            os.link(canary, link)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.read_tree(workspace)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.write_files(workspace, {"link": b"bad"})
            self.assertEqual(canary.read_bytes(), b"unchanged")

    def test_directory_symlink_cannot_redirect_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            target = root / "target"
            target.mkdir()
            (target / "subdir").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(sandbox.SandboxError):
                sandbox.write_files(target, {"subdir/new": b"bad"})
            self.assertEqual(list(outside.iterdir()), [])


class LaunchBoundaryTests(unittest.TestCase):
    def test_gateway_receipt_requires_complete_successful_drain(self):
        ready = b'{"event":"ready","role":"broker"}\n'
        stopped = b'{"event":"stopped","role":"broker","clean":true}\n'
        self.assertEqual(len(sandbox.validate_gateway_ledger(ready + stopped, started=True, exit_code=0)), 64)
        self.assertEqual(len(sandbox.validate_gateway_ledger(b"", started=False, exit_code=137)), 64)
        for ledger, code in ((ready, 0), (ready + stopped, 137), (stopped.rstrip(), 0),
                             (b"broken\n", 0), (b"[]\n", 0),
                             (stopped.replace(b"true", b"false"), 0)):
            with self.subTest(ledger=ledger, code=code), self.assertRaises(sandbox.SandboxError):
                sandbox.validate_gateway_ledger(ledger, started=True, exit_code=code)

    def test_candidate_rejection_keeps_confirmed_boundary_receipt(self):
        receipt = {"solver_stopped": True, "broker_revoked": True}
        error = sandbox.SandboxArtifactError("source link rejected", receipt)
        receipt["solver_stopped"] = False
        self.assertTrue(error.receipt["solver_stopped"])
        self.assertIsInstance(error, sandbox.SandboxError)

    def test_solver_environment_accepts_dummy_route_and_rejects_credentials_and_host_paths(self):
        allowed = {"CODEX_HOME": "/tmp/codex-home", "HOME": "/home/solver",
                   "OPENAI_BASE_URL": "http://127.0.0.1:8765/", "OPENAI_API_KEY": "openbench-sandbox-placeholder"}
        self.assertEqual(sandbox.solver_env(allowed), allowed)
        for bad in ({"CODEX_AUTH_JSON_PATH": "/private/staged/auth.json"},
                    {"OPENAI_API_KEY": "real-looking-secret"}, {"SSH_AUTH_SOCK": "/ssh-agent"},
                    {"OPENAI_BASE_URL": "https://provider.example"},
                    {"HOME": "/home/solver-other"}, {"CODEX_HOME": "/tmp/../root/.codex"}):
            with self.subTest(keys=list(bad)), self.assertRaises(sandbox.SandboxError):
                sandbox.solver_env(bad)

    def test_generated_launch_has_no_host_binds_and_only_gateway_is_shared(self):
        config = sandbox.compose_config(IMAGE, TOKEN)
        solver, broker = config["services"]["main"], config["services"]["broker"]
        self.assertEqual(solver["network_mode"], "none")
        self.assertEqual(solver["user"], "10001:10001")
        self.assertEqual(solver["cap_drop"], ["ALL"])
        self.assertTrue(solver["read_only"])
        solver_volumes = {v["source"] for v in solver["volumes"]}
        broker_volumes = {v["source"] for v in broker["volumes"]}
        self.assertEqual(solver_volumes & broker_volumes, {"gateway"})
        self.assertTrue(next(v for v in solver["volumes"] if v["source"] == "gateway")["read_only"])
        self.assertTrue(all(v["type"] == "volume" for service in config["services"].values() for v in service["volumes"]))

    def test_mutable_images_and_unbounded_resources_are_rejected(self):
        for image in ("python:latest", "python", "sha256:short", "x@sha256:" + "g" * 64):
            with self.subTest(image=image), self.assertRaises(sandbox.SandboxError):
                sandbox.compose_config(image, TOKEN)
        for values in ({"cpus": 0}, {"cpus": 128}, {"memory_mb": 0}, {"memory_mb": 65536}):
            with self.subTest(values=values), self.assertRaises(sandbox.SandboxError):
                sandbox.compose_config(IMAGE, TOKEN, **values)

    def test_effective_inspection_detects_network_privilege_mount_resource_drift(self):
        volumes = {key: value["name"] for key, value in sandbox.compose_config(IMAGE, TOKEN)["volumes"].items()}
        good = {"Image": IMAGE, "Config": {"User": "10001:10001"},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                    "IpcMode": "private", "Memory": 2048 * 1024**2,
                    "NanoCpus": 2_000_000_000, "PidsLimit": 256},
                "Mounts": [{"Type": "volume", "Name": volumes[key], "Destination": target, "RW": rw}
                           for key, target, rw in (("source", "/app", True), ("logs", "/logs", True),
                                                    ("gateway", "/run/openbench-model", False))]}
        sandbox.verify_inspection(good, role="main", image_id=IMAGE, volume_names=volumes)
        for field, value in (("NetworkMode", "host"), ("Privileged", True), ("CapAdd", ["NET_RAW"]),
                             ("CapDrop", []), ("PidMode", "host"), ("Devices", [{"PathOnHost": "/dev/x"}]),
                             ("Memory", 0), ("NanoCpus", 0), ("SecurityOpt", [])):
            bad = copy.deepcopy(good)
            bad["HostConfig"][field] = value
            with self.subTest(field=field), self.assertRaises(sandbox.SandboxError):
                sandbox.verify_inspection(bad, role="main", image_id=IMAGE, volume_names=volumes)
        bad = copy.deepcopy(good)
        bad["Mounts"][2]["RW"] = True
        with self.assertRaises(sandbox.SandboxError):
            sandbox.verify_inspection(bad, role="main", image_id=IMAGE, volume_names=volumes)

    def test_module_import_does_not_require_or_load_harbor(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import obench.harbor_sandbox; "
                                "assert not any(x == 'harbor' or x.startswith('harbor.') for x in sys.modules)"],
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
