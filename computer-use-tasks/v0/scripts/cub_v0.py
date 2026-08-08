#!/usr/bin/env python3
"""Build, configure, and safely reset Computer-Use Bench v0 native trials.

The default ``preflight`` command is read-only. Build, setup, and reset are
explicit subcommands so inspecting readiness never launches or changes an app.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from typing import Any, Callable, Iterable, Mapping, Sequence

# Keep the checked-in standalone command usable before OpenBench is installed.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from obench.native_matrix import build_native_matrix, canonical_bytes
from obench.native_run import _canonical_digest, _content_bound_command_digest


ROOT = Path(__file__).resolve().parents[1]
BASIC_REVISION = "2c5cc162e58f6486505c8c5fe87fd76980d0e6b9"
SOURCE_REVISION = "38e517261a1d33b224a948e0a0b7cb94196bb389"
MCP_VERSION = "0.4.1"
SOURCE_MCP_BUNDLE_ID = "org.openbench.computer-use-mcp.source.v041"
INSTALLED_MCP_BUNDLE_ID = "dev.computer-use-mcp.app"
FIXTURE_BUNDLES = {
    "basic-controls": "org.openbench.ComputerUseFixture.v0",
    "state-response-ab": "org.openbench.ComputerUseFixture.v0",
    "background-control": "org.openbench.BackgroundControlFixture.v0",
    "textedit-exact-file": "com.apple.TextEdit",
}
GUARD_BUNDLE_ID = "org.openbench.FocusGuard.v0"
TASKS = tuple(FIXTURE_BUNDLES)
MATCHED_TASKS = tuple(task for task in TASKS if task != "state-response-ab")
ARMS = ("installed", "source")
STATE_RESPONSE_ARMS = ("auto", "full")
CALL_CONTRACT = (
    {"tool": "get_app_state", "required_arguments": {
        "app": "ComputerUseFixture", "include_screenshot": False,
    }},
    {"tool": "click", "required_arguments": {
        "include_state": True, "include_screenshot": False,
    }},
    {"tool": "click", "required_arguments": {
        "include_state": True, "include_screenshot": False,
    }},
    {"tool": "click", "required_arguments": {
        "include_state": True, "include_screenshot": False,
    }},
    {"tool": "set_value", "required_arguments": {
        "value": "openbench-42", "include_state": True,
        "include_screenshot": False,
    }},
)


def state_call_contract(mode: str) -> list[dict[str, Any]]:
    if mode not in STATE_RESPONSE_ARMS:
        raise CubError("state response mode must be auto or full")
    return [
        {
            "tool": item["tool"],
            "required_arguments": {
                **item["required_arguments"],
                **(
                    {"state_response_mode": mode}
                    if item["tool"] != "get_app_state"
                    else {}
                ),
            },
        }
        for item in CALL_CONTRACT
    ]
DELIVERY_TIERS = {
    "basic-controls": (
        "tier1-ax-action",
        "tier1-ax-attribute",
        "tier2-per-window-nsevent",
        "tier25-skylight-sleventpostto-pid",
        "tier3-cgeventpostto-pid",
        "pasteboard",
        "launchservices",
        "ax-window-management",
    ),
    "state-response-ab": (
        "tier1-ax-action",
        "tier1-ax-attribute",
        "tier2-per-window-nsevent",
        "tier25-skylight-sleventpostto-pid",
        "tier3-cgeventpostto-pid",
        "pasteboard",
        "launchservices",
        "ax-window-management",
    ),
    "background-control": (
        "tier1-ax-action",
        "tier1-ax-attribute",
        "tier2-per-window-nsevent",
    ),
    "textedit-exact-file": (
        "tier1-ax-action",
        "tier1-ax-attribute",
        "tier2-per-window-nsevent",
        "tier25-skylight-sleventpostto-pid",
        "tier3-cgeventpostto-pid",
        "pasteboard",
        "launchservices",
        "ax-window-management",
    ),
}
CONFIG_SCHEMA = "openbench.computer-use-config-request.v1"
PREFLIGHT_SCHEMA = "openbench.computer-use-preflight.v1"
PROCESS_SCHEMA = "openbench.computer-use-process-state.v1"
RUN_CONTEXT_SCHEMA = "openbench.computer-use.run-context.v1"
ENV_VALUE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


class CubError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _positive_trial_index(value: Any) -> int:
    if isinstance(value, bool):
        raise CubError("trial index must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CubError("trial index must be a positive integer") from exc
    if parsed < 1 or str(parsed) != str(value):
        raise CubError("trial index must be a positive integer")
    return parsed


def _positive_trial_argument(value: str) -> int:
    try:
        return _positive_trial_index(value)
    except CubError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _runtime_coordinates(
    arm: str | None, task: str | None, trial_index: int | None
) -> tuple[str, str, int]:
    supplied = (arm is not None, task is not None, trial_index is not None)
    if any(supplied) and not all(supplied):
        raise CubError("runtime arm, task, and trial index must be supplied together")
    if all(supplied):
        return str(arm), str(task), _positive_trial_index(trial_index)
    trial_id = os.environ.get("OPENBENCH_NATIVE_TRIAL_ID", "")
    task_id = os.environ.get("OPENBENCH_NATIVE_TASK_ID", "")
    collector_id = os.environ.get("OPENBENCH_NATIVE_MCP_COLLECTOR_RUN_ID", "")
    match = re.search(r"(?:^|[-_:])trial([1-9][0-9]*)$", trial_id)
    task_prefix = "openbench/computer-use-v0-"
    if (
        match is None
        or not task_id.startswith(task_prefix)
        or collector_id not in {
            f"cub-v0-{item}-mcp" for item in (*ARMS, *STATE_RESPONSE_ARMS)
        }
    ):
        raise CubError("runtime coordinates are absent from explicit args/native env")
    resolved_task = task_id.removeprefix(task_prefix)
    if resolved_task not in TASKS:
        raise CubError("native task identity is not a Computer-Use Bench v0 task")
    resolved_arm = collector_id.removeprefix("cub-v0-").removesuffix("-mcp")
    return resolved_arm, resolved_task, int(match.group(1))


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()[-1200:]
        raise CubError(f"command failed: {list(argv)!r}: {detail or exc}") from exc


def safe_run_root(value: str | os.PathLike[str]) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise CubError("run root must be absolute")
    root = raw.resolve(strict=False)
    forbidden = {Path("/"), Path.home(), Path.home() / "Desktop", Path.home() / "Documents"}
    if root in forbidden or len(root.parts) < 4:
        raise CubError(f"unsafe run root: {root}")
    if raw.exists() and raw.is_symlink():
        raise CubError("run root cannot be a symlink")
    return root


def descendant(root: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    path = path if path.is_absolute() else root / path
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CubError(f"path escapes run root: {resolved}") from exc
    if resolved == root:
        raise CubError("operation requires a child of the run root")
    return resolved


def _load_request(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CubError(f"cannot load request {path}: {exc}") from exc
    if value.get("schema_version") != CONFIG_SCHEMA:
        raise CubError(f"request schema_version must be {CONFIG_SCHEMA}")
    for field, item in tuple(value.items()):
        if not isinstance(item, str):
            continue
        match = ENV_VALUE.fullmatch(item)
        if match:
            name = match.group(1)
            replacement = os.environ.get(name)
            if not replacement:
                raise CubError(f"request environment variable is unset: {name}")
            value[field] = replacement
        elif "${" in item:
            raise CubError(f"request {field} contains an unsupported environment expression")
    required = ("run_root", "computer_use_mcp_repo", "installed_mcp_app")
    for field in required:
        if not isinstance(value.get(field), str) or not value[field]:
            raise CubError(f"request {field} must be a non-empty string")
    return value


def _request_paths(request: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    root = safe_run_root(request["run_root"])
    repo = Path(request["computer_use_mcp_repo"]).expanduser().resolve()
    installed = Path(request["installed_mcp_app"]).expanduser().resolve()
    return root, repo, installed


def _git_has_commit(repo: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=repo,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _bundle_info(app: Path) -> dict[str, Any]:
    plist = app / "Contents/Info.plist"
    with plist.open("rb") as handle:
        info = plistlib.load(handle)
    executable = app / "Contents/MacOS" / str(info["CFBundleExecutable"])
    if not executable.is_file() or executable.is_symlink():
        raise CubError(f"bundle executable is unavailable: {executable}")
    requirement = _run(["codesign", "-d", "-r-", str(app)], timeout=30)
    requirement_output = (requirement.stdout + requirement.stderr).strip()
    designated = next(
        (line.removeprefix("# ").removeprefix("designated =>").strip()
         for line in requirement_output.splitlines()
         if line.startswith(("# designated =>", "designated =>"))),
        "",
    )
    if not designated:
        raise CubError(f"bundle has no designated requirement: {app}")
    details = _run(["codesign", "-dvvv", str(app)], timeout=30)
    codesign_text = (details.stderr or details.stdout).strip()
    binary_sha256 = _sha256(executable)
    return {
        "app": str(app),
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build": str(info.get("CFBundleVersion", "")),
        "executable": str(executable.resolve()),
        "binary_sha256": binary_sha256,
        "build_stamp_unix": executable.stat().st_mtime_ns,
        "designated_requirement": designated,
        "signature_sha256": hashlib.sha256(
            designated.encode("utf-8") + b"\0" + binary_sha256.encode("ascii")
        ).hexdigest(),
        "adhoc": "flags=0x2(adhoc)" in codesign_text or "Signature=adhoc" in codesign_text,
    }


def _static_preflight(request: Mapping[str, Any]) -> dict[str, Any]:
    root, repo, installed = _request_paths(request)
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, observed: Any, required: Any) -> None:
        checks.append({"name": name, "passed": passed, "observed": observed, "required": required})

    add("run_root_safe", True, str(root), "absolute non-user-document run root")
    add("computer_use_mcp_repo", repo.is_dir(), str(repo), "existing directory")
    if repo.is_dir():
        for revision in (BASIC_REVISION, SOURCE_REVISION):
            add(f"source_commit:{revision}", _git_has_commit(repo, revision), revision, "present")
    codex_path = shutil.which("codex")
    codex_version = None
    if codex_path:
        try:
            codex_version = _run([codex_path, "--version"], timeout=10).stdout.strip()
        except CubError:
            pass
    add(
        "codex_native_profile",
        codex_version == request.get("codex_version", "codex-cli 0.146.1"),
        {"path": codex_path, "version": codex_version},
        {"version": request.get("codex_version", "codex-cli 0.146.1")},
    )
    auth_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    add("codex_auth", auth_path.is_file(), str(auth_path), "existing auth.json")
    try:
        installed_identity = _bundle_info(installed)
    except (CubError, OSError, KeyError, plistlib.InvalidFileException) as exc:
        installed_identity = None
        add("installed_mcp_identity", False, str(exc), "valid signed 0.4.1 bundle")
    else:
        add(
            "installed_mcp_identity",
            installed_identity["bundle_id"] == INSTALLED_MCP_BUNDLE_ID
            and installed_identity["version"] == MCP_VERSION,
            installed_identity,
            {"bundle_id": INSTALLED_MCP_BUNDLE_ID, "version": MCP_VERSION},
        )
    source_app = root / "apps/OpenBench Computer Use MCP Source.app"
    try:
        source_identity = _bundle_info(source_app)
    except (CubError, OSError, KeyError, plistlib.InvalidFileException) as exc:
        source_identity = None
        add("source_mcp_identity", False, str(exc), "built signed source bundle")
    else:
        add(
            "source_mcp_identity",
            source_identity["bundle_id"] == SOURCE_MCP_BUNDLE_ID
            and source_identity["version"] == MCP_VERSION
            and not source_identity["adhoc"],
            source_identity,
            {"bundle_id": SOURCE_MCP_BUNDLE_ID, "version": MCP_VERSION, "adhoc": False},
        )
    for arm, identity, bundle_id in (
        ("installed", installed_identity, INSTALLED_MCP_BUNDLE_ID),
        ("source", source_identity, SOURCE_MCP_BUNDLE_ID),
    ):
        permission_proof = root / f"preflight/{arm}-permissions.json"
        permission_ok = False
        permission_observed: Any = "missing"
        if permission_proof.is_file() and identity is not None:
            try:
                proof = json.loads(permission_proof.read_text(encoding="utf-8"))
                permission_observed = proof
                permission_ok = (
                    proof.get("schema_version") == PREFLIGHT_SCHEMA
                    and proof.get("binary_sha256") == identity["binary_sha256"]
                    and proof.get("bundle_id") == bundle_id
                    and proof.get("accessibility") == "granted"
                    and proof.get("screen_recording") == "granted"
                    and proof.get("capture_status") == "responsive"
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                permission_observed = "invalid JSON"
        add(
            f"{arm}_tcc_identity_proof", permission_ok, permission_observed,
            "health proof bound to this exact app binary",
        )
    return {
        "schema_version": PREFLIGHT_SCHEMA,
        "read_only": True,
        "matched_ready": all(item["passed"] for item in checks),
        "checks": checks,
        "next": {
            "build": f"{sys.executable} {Path(__file__).resolve()} --request REQUEST.toml build",
            "permission_probes": [
                f"{sys.executable} {Path(__file__).resolve()} --request REQUEST.toml probe-permissions --arm installed",
                f"{sys.executable} {Path(__file__).resolve()} --request REQUEST.toml probe-permissions --arm source",
            ],
        },
    }


def _publication_safe_preflight(result: Mapping[str, Any]) -> dict[str, Any]:
    safe_checks = []
    for check in result["checks"]:
        name = check["name"]
        observed = check["observed"]
        if name == "run_root_safe":
            observed = "$OPENBENCH_CUB_RUN_ROOT"
        elif name == "computer_use_mcp_repo":
            observed = {"configured": True, "exists": check["passed"]}
        elif name == "codex_native_profile":
            observed = {
                "available": bool(observed.get("path")),
                "version": observed.get("version"),
            }
        elif name == "codex_auth":
            observed = {"present": check["passed"]}
        elif name in {"installed_mcp_identity", "source_mcp_identity"}:
            if isinstance(observed, dict):
                observed = {
                    field: observed[field]
                    for field in (
                        "bundle_id", "version", "build", "binary_sha256",
                        "build_stamp_unix", "signature_sha256", "adhoc",
                    )
                    if field in observed
                }
            else:
                observed = {"available": False}
        elif name.endswith("_tcc_identity_proof"):
            if isinstance(observed, dict):
                observed = {
                    field: observed[field]
                    for field in (
                        "schema_version", "arm", "bundle_id", "binary_sha256",
                        "accessibility", "screen_recording", "capture_status",
                    )
                    if field in observed
                }
        safe_checks.append({
            "name": name,
            "passed": check["passed"],
            "observed": observed,
            "required": check["required"],
        })
    return {
        "schema_version": result["schema_version"],
        "read_only": True,
        "publication_safe": True,
        "matched_ready": result["matched_ready"],
        "checks": safe_checks,
        "next": {
            "build": (
                "python3 computer-use-tasks/v0/scripts/cub_v0.py "
                '--request "$OPENBENCH_CUB_REQUEST" build'
            ),
            "permission_probes": [
                (
                    "python3 computer-use-tasks/v0/scripts/cub_v0.py "
                    f'--request "$OPENBENCH_CUB_REQUEST" probe-permissions --arm {arm}'
                )
                for arm in ARMS
            ],
        },
    }


def preflight(request_path: Path) -> int:
    result = _static_preflight(_load_request(request_path))
    print(json.dumps(_publication_safe_preflight(result), indent=2, sort_keys=True))
    return 0 if result["matched_ready"] else 2


def _extract_revision(repo: Path, revision: str, destination: Path) -> None:
    if destination.exists():
        marker = destination / ".openbench-source-commit"
        if marker.is_file() and marker.read_text(encoding="ascii").strip() == revision:
            return
        raise CubError(f"existing source destination is not pinned to {revision}: {destination}")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision], cwd=repo,
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
    )
    if archive.returncode != 0:
        raise CubError(f"cannot archive source commit {revision}: {archive.stderr.decode(errors='replace')}")
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        for member in tar.getmembers():
            target = descendant(destination, member.name)
            if member.issym() or member.islnk() or target == destination:
                raise CubError(f"unsafe source archive member: {member.name}")
        tar.extractall(destination, filter="data")
    observed = _run(["git", "rev-parse", revision], cwd=repo).stdout.strip()
    (destination / ".openbench-source-commit").write_text(observed + "\n", encoding="ascii")


def _wrap_app(binary: Path, app: Path, bundle_id: str, version: str, identity: str) -> None:
    if app.exists():
        current = _bundle_info(app)
        if (
            current["bundle_id"] == bundle_id
            and current["version"] == version
            and current["binary_sha256"] == _sha256(binary)
        ):
            return
        raise CubError(f"existing app does not match the exact built executable: {app}")
    macos = app / "Contents/MacOS"
    macos.mkdir(parents=True)
    target = macos / binary.name
    shutil.copy2(binary, target)
    target.chmod(0o755)
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": app.stem,
        "CFBundleExecutable": binary.name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": app.stem,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.0",
    }
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(info, handle, sort_keys=True)
    _run(["codesign", "--force", "--deep", "--sign", identity, str(app)], timeout=60)


GUARD_SOURCE = r'''import AppKit
final class Delegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let label = NSTextField(labelWithString: "OpenBench Focus Guard")
        label.frame = NSRect(x: 28, y: 45, width: 260, height: 28)
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 120))
        content.addSubview(label)
        window = NSWindow(contentRect: content.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "OpenBench Focus Guard"
        window.contentView = content
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }
}
let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
'''


def _build_manifest(source_app: Path, apps: Path) -> dict[str, Any]:
    return {
        "schema_version": "openbench.computer-use-build.v1",
        "basic_fixture_revision": BASIC_REVISION,
        "source_revision": SOURCE_REVISION,
        "source_mcp": _bundle_info(source_app),
        "fixtures": {
            task: _bundle_info(app)
            for task, app in (
                ("basic-controls", apps / "ComputerUseFixture.app"),
                ("state-response-ab", apps / "ComputerUseFixture.app"),
                (
                    "background-control",
                    apps / "BackgroundControlFixture.app",
                ),
                ("guard", apps / "FocusGuard.app"),
                (
                    "textedit-exact-file",
                    Path("/System/Applications/TextEdit.app"),
                ),
            )
        },
    }


def build(request_path: Path) -> int:
    request = _load_request(request_path)
    root, repo, _installed = _request_paths(request)
    identity = request.get("source_signing_identity")
    if not isinstance(identity, str) or not identity or identity == "-":
        raise CubError("source_signing_identity must be a stable non-ad-hoc codesigning identity")
    root.mkdir(parents=True, exist_ok=True)
    sources = descendant(root, "sources")
    apps = descendant(root, "apps")
    build_root = descendant(root, "build")
    for path in (sources, apps, build_root):
        path.mkdir(parents=True, exist_ok=True)

    basic_source = sources / f"computer-use-mcp-{BASIC_REVISION}"
    source_tree = sources / f"computer-use-mcp-{SOURCE_REVISION}"
    _extract_revision(repo, BASIC_REVISION, basic_source)
    _extract_revision(repo, SOURCE_REVISION, source_tree)

    basic_scratch = build_root / "basic"
    _run([
        "swift", "build", "--package-path", str(basic_source), "--scratch-path",
        str(basic_scratch), "-c", "release", "--product", "ComputerUseFixture",
    ], timeout=1200)
    basic_bin = Path(_run([
        "swift", "build", "--package-path", str(basic_source), "--scratch-path",
        str(basic_scratch), "-c", "release", "--show-bin-path",
    ]).stdout.strip()) / "ComputerUseFixture"
    _wrap_app(
        basic_bin, apps / "ComputerUseFixture.app", FIXTURE_BUNDLES["basic-controls"],
        "0.0.1", "-",
    )

    background_bin = build_root / "BackgroundControlFixture"
    _run([
        "xcrun", "swiftc", str(source_tree / "scripts/fixtures/BackgroundControlFixture.swift"),
        "-framework", "AppKit", "-O", "-o", str(background_bin),
    ], timeout=600)
    _wrap_app(
        background_bin, apps / "BackgroundControlFixture.app",
        FIXTURE_BUNDLES["background-control"], "0.0.1", "-",
    )

    guard_source = build_root / "FocusGuard.swift"
    guard_source.write_text(GUARD_SOURCE, encoding="utf-8")
    guard_bin = build_root / "FocusGuard"
    _run(["xcrun", "swiftc", str(guard_source), "-framework", "AppKit", "-O", "-o", str(guard_bin)])
    _wrap_app(guard_bin, apps / "FocusGuard.app", GUARD_BUNDLE_ID, "0.0.1", "-")

    result = _run([
        sys.executable, str(source_tree / "scripts/build_app_bundle.py"),
        "--app-name", "OpenBench Computer Use MCP Source",
        "--bundle-id", SOURCE_MCP_BUNDLE_ID,
        "--configuration", "release", "--identity", identity,
        "--install", str(apps),
    ], cwd=source_tree, timeout=1800)
    build_result = json.loads(result.stdout)
    source_app = apps / "OpenBench Computer Use MCP Source.app"
    if Path(str(build_result.get("installed_bundle"))).resolve() != source_app.resolve():
        raise CubError("source build installed an unexpected app bundle")
    manifest = _build_manifest(source_app, apps)
    manifest_path = root / "build-manifest.json"
    manifest_path.write_text(_canonical(manifest) + "\n", encoding="utf-8")
    print(json.dumps({"build_manifest": str(manifest_path)}, sort_keys=True))
    return 0


def probe_permissions(request_path: Path, arm: str) -> int:
    request = _load_request(request_path)
    root, _repo, installed = _request_paths(request)
    app = installed if arm == "installed" else root / "apps/OpenBench Computer Use MCP Source.app"
    identity = _bundle_info(app)
    completed = _run([
        identity["executable"], "health_report", "--json", "--probe-capture"
    ], timeout=30)
    try:
        health = json.loads(completed.stdout)
        permissions = health["permissions"]
        proof = {
            "schema_version": PREFLIGHT_SCHEMA,
            "arm": arm,
            "bundle_id": identity["bundle_id"],
            "binary_sha256": identity["binary_sha256"],
            "accessibility": permissions["accessibility"]["status"],
            "screen_recording": permissions["screenRecording"]["status"],
            "capture_status": health["captureService"]["status"],
        }
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CubError(f"invalid health report: {exc}") from exc
    path = descendant(root, f"preflight/{arm}-permissions.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps(proof, sort_keys=True))
    return 0 if all(
        proof[field] == expected for field, expected in (
            ("accessibility", "granted"),
            ("screen_recording", "granted"),
            ("capture_status", "responsive"),
        )
    ) else 2


def _process_identity(pid: int) -> dict[str, Any] | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        return None
    match = re.match(r"^(.{24})\s+(.+)$", line)
    if not match:
        raise CubError(f"cannot parse process identity for pid {pid}")
    command = match.group(2)
    return {"pid": pid, "start_token": match.group(1), "command": command}


def terminate_owned(
    state_path: Path,
    *,
    identity_reader: Callable[[int], dict[str, Any] | None] = _process_identity,
    signaler: Callable[[int, int], None] = os.kill,
) -> None:
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CubError(f"invalid process state; refusing cleanup: {exc}") from exc
    if state.get("schema_version") != PROCESS_SCHEMA or not isinstance(state.get("processes"), list):
        raise CubError("invalid process state schema; refusing cleanup")
    owned: list[tuple[int, dict[str, Any]]] = []
    for record in reversed(state["processes"]):
        pid = record.get("pid")
        if type(pid) is not int or pid <= 0:
            raise CubError("invalid owned pid; refusing cleanup")
        observed = identity_reader(pid)
        if observed is None:
            continue
        if (
            observed.get("start_token") != record.get("start_token")
            or observed.get("command") != record.get("command")
        ):
            raise CubError(f"pid {pid} no longer matches owned process; refusing to signal")
        signaler(pid, signal.SIGTERM)
        owned.append((pid, record))
    deadline = time.monotonic() + 2.0
    while owned and time.monotonic() < deadline:
        owned = [
            (pid, record) for pid, record in owned
            if identity_reader(pid) is not None
        ]
        if owned:
            time.sleep(0.05)
    for pid, record in owned:
        observed = identity_reader(pid)
        if observed is None:
            continue
        if (
            observed.get("start_token") != record.get("start_token")
            or observed.get("command") != record.get("command")
        ):
            raise CubError(f"pid {pid} changed during cleanup; refusing to signal")
        signaler(pid, signal.SIGKILL)
    state_path.unlink()


def _state_path(root: Path, arm: str, task: str, trial_index: int) -> Path:
    trial_index = _positive_trial_index(trial_index)
    return descendant(root, f"runtime/{task}/trial{trial_index}/{arm}/processes.json")


def _workspace(root: Path, arm: str, task: str, trial_index: int) -> Path:
    trial_index = _positive_trial_index(trial_index)
    return descendant(root, f"workspaces/{task}/trial{trial_index}/{arm}")


def _evidence(root: Path, arm: str, task: str, trial_index: int) -> Path:
    trial_index = _positive_trial_index(trial_index)
    return descendant(root, f"runner-evidence/{task}/trial{trial_index}/{arm}")


def _result_paths(
    root: Path, mode: str, arm: str, task: str, trial_index: int
) -> tuple[Path, Path]:
    trial_index = _positive_trial_index(trial_index)
    result_root = descendant(
        root, f"results/{mode}/{task}/trial{trial_index}/{arm}"
    )
    return result_root / "bundle", result_root / "result.jsonl"


def _initial_state_ready(root: Path, arm: str, task: str, trial_index: int) -> bool:
    workspace = _workspace(root, arm, task, trial_index)
    if task == "textedit-exact-file":
        output = workspace / "artifacts/openbench-exact.txt"
        return (workspace / "run-context.json").is_file() and not output.exists()
    state_path = workspace / "artifacts/fixture-state.json"
    expected = {
        "basic-controls": {
            "fixture": "basic-controls", "honest_counter": 0,
            "keystroke_echo": "", "schema_version": 1, "toggle_on": False,
        },
        "state-response-ab": {
            "fixture": "basic-controls", "honest_counter": 0,
            "keystroke_echo": "", "schema_version": 1, "toggle_on": False,
        },
        "background-control": {
            "button_status": "idle", "fixture": "background-control",
            "menu_status": "idle", "schema_version": 1,
            "text_field": "initial-background-value",
        },
    }
    try:
        return json.loads(state_path.read_text(encoding="utf-8")) == expected[task]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _launch(executable: Path, args: Sequence[str], env: Mapping[str, str], log: Path) -> dict[str, Any]:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("ab", buffering=0)
    process = subprocess.Popen(
        [str(executable), *args], env=dict(env), stdin=subprocess.DEVNULL,
        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
    )
    handle.close()
    time.sleep(0.25)
    if process.poll() is not None:
        raise CubError(f"owned process exited during launch: {executable}")
    identity = _process_identity(process.pid)
    if identity is None:
        raise CubError(f"cannot prove launched process identity: {process.pid}")
    return identity


def _frontmost_bundle_id() -> str | None:
    asn = _run(["lsappinfo", "front"], timeout=5).stdout.strip()
    if not asn:
        return None
    info = _run(["lsappinfo", "info", "-only", "bundleID", asn], timeout=5).stdout
    match = re.search(r'"CFBundleIdentifier"="([^"]+)"', info)
    return match.group(1) if match else None


def _wait_for_frontmost(
    expected: str,
    *,
    timeout_s: float = 5.0,
    probe: Callable[[], str | None] = _frontmost_bundle_id,
) -> None:
    deadline = time.monotonic() + timeout_s
    observed = probe()
    while observed != expected and time.monotonic() < deadline:
        time.sleep(0.05)
        observed = probe()
    if observed != expected:
        raise CubError(
            f"setup did not establish required foreground app {expected!r}; "
            f"observed {observed!r}"
        )


def reset_runtime(request_path: Path, arm: str, task: str, trial_index: int) -> int:
    trial_index = _positive_trial_index(trial_index)
    request = _load_request(request_path)
    root, _repo, _installed = _request_paths(request)
    state = _state_path(root, arm, task, trial_index)
    terminate_owned(state)
    workspace = _workspace(root, arm, task, trial_index)
    evidence = _evidence(root, arm, task, trial_index)
    for relative in ("artifacts", "runner", "trajectory.json", "codex-events.jsonl"):
        target = descendant(root, workspace / relative)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    if evidence.is_dir():
        shutil.rmtree(evidence)
    return 0


def setup(request_path: Path, arm: str, task: str, trial_index: int) -> int:
    trial_index = _positive_trial_index(trial_index)
    request = _load_request(request_path)
    root, _repo, _installed = _request_paths(request)
    state_path = _state_path(root, arm, task, trial_index)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            state = {}
        records = state.get("processes") if state.get("schema_version") == PROCESS_SCHEMA else None
        if isinstance(records, list) and records and all(
            _process_identity(record.get("pid")) == record for record in records
        ) and _initial_state_ready(root, arm, task, trial_index):
            return 0
    reset_runtime(request_path, arm, task, trial_index)
    workspace = _workspace(root, arm, task, trial_index)
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts = workspace / "artifacts"
    artifacts.mkdir()
    logs = descendant(root, f"runtime/{task}/trial{trial_index}/{arm}/logs")
    apps = root / "apps"
    processes: list[dict[str, Any]] = []
    state_path.parent.mkdir(parents=True, exist_ok=True)

    def remember(record: dict[str, Any]) -> None:
        processes.append(record)
        state_path.write_text(
            _canonical({"schema_version": PROCESS_SCHEMA, "processes": processes}) + "\n",
            encoding="utf-8",
        )

    env = os.environ.copy()
    if task in {"basic-controls", "state-response-ab"}:
        state = artifacts / "fixture-state.json"
        env.update({
            "COMPUTER_USE_FIXTURE_STATE_PATH": str(state),
            "COMPUTER_USE_FIXTURE_FOREGROUND": "1",
        })
        executable = apps / "ComputerUseFixture.app/Contents/MacOS/ComputerUseFixture"
        remember(_launch(executable, (), env, logs / "fixture.log"))
        foreground_bundle_id = FIXTURE_BUNDLES[task]
    elif task == "background-control":
        state = artifacts / "fixture-state.json"
        env["COMPUTER_USE_FIXTURE_STATE_PATH"] = str(state)
        fixture = apps / "BackgroundControlFixture.app/Contents/MacOS/BackgroundControlFixture"
        guard = apps / "FocusGuard.app/Contents/MacOS/FocusGuard"
        remember(_launch(fixture, (), env, logs / "fixture.log"))
        remember(_launch(guard, (), os.environ.copy(), logs / "guard.log"))
        foreground_bundle_id = GUARD_BUNDLE_ID
    else:
        relative = "artifacts/openbench-exact.txt"
        (workspace / "run-context.json").write_text(
            _canonical({"schema_version": RUN_CONTEXT_SCHEMA, "output_path": relative}) + "\n",
            encoding="utf-8",
        )
        before = {
            int(item) for item in _run(["pgrep", "-x", "TextEdit"], timeout=10).stdout.split()
        } if subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True).returncode == 0 else set()
        _run([
            "open", "-n", "-a", "TextEdit", "--args",
            "-ApplePersistenceIgnoreState", "YES", "-NSQuitAlwaysKeepsWindows", "NO",
        ], timeout=30)
        deadline = time.monotonic() + 10
        new: set[int] = set()
        while time.monotonic() < deadline:
            probe = subprocess.run(["pgrep", "-x", "TextEdit"], capture_output=True, text=True)
            current = {int(item) for item in probe.stdout.split()} if probe.returncode == 0 else set()
            new = current - before
            if len(new) == 1:
                break
            time.sleep(0.1)
        if len(new) != 1:
            raise CubError("could not prove one newly launched TextEdit process")
        identity = _process_identity(new.pop())
        if identity is None:
            raise CubError("new TextEdit process disappeared")
        remember(identity)
        foreground_bundle_id = FIXTURE_BUNDLES[task]
    _wait_for_frontmost(foreground_bundle_id)
    return 0


def verify(request_path: Path, arm: str, task: str, trial_index: int) -> int:
    trial_index = _positive_trial_index(trial_index)
    request = _load_request(request_path)
    root, _repo, _installed = _request_paths(request)
    workspace = _workspace(root, arm, task, trial_index)
    runner = workspace / "runner"
    env = os.environ.copy()
    env["TASK_DIR"] = str(ROOT / task)
    if task == "textedit-exact-file":
        env["OPENBENCH_NATIVE_OUTPUT_PATH"] = str(workspace / "artifacts/openbench-exact.txt")
        command = ["bash", str(ROOT / task / "checker.sh")]
    else:
        state = workspace / "artifacts/fixture-state.json"
        if task in {"basic-controls", "state-response-ab"}:
            env["OPENBENCH_FIXTURE_STATE_PATH"] = str(state)
            command = ["bash", str(ROOT / task / "checker.sh")]
        else:
            command = [
                sys.executable, "-c",
                "import importlib.util,sys;"
                "s=importlib.util.spec_from_file_location('cub_bg_verify',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.verify_state(sys.argv[2])",
                str(ROOT / task / "checker_data/verify.py"), str(state),
            ]
    result = subprocess.run(
        command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30, check=False,
    )
    verdict = {
        "checker_exit": result.returncode,
        "score": 1.0 if result.returncode == 0 else 0.0,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }
    runner.mkdir(exist_ok=True)
    (runner / "verdict.json").write_text(_canonical(verdict) + "\n", encoding="utf-8")
    return result.returncode


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _toml_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_inline_table(value: Mapping[str, Any]) -> str:
    parts = []
    for key, item in value.items():
        if isinstance(item, bool):
            rendered = "true" if item else "false"
        elif isinstance(item, str):
            rendered = _toml_string(item)
        else:
            raise CubError(f"unsupported call contract value for {key!r}")
        parts.append(f"{key} = {rendered}")
    return "{ " + ", ".join(parts) + " }"


def _write_immutable_outputs(outputs: Mapping[Path, bytes]) -> None:
    for path, payload in outputs.items():
        if path.is_symlink():
            raise CubError(f"immutable output cannot be a symlink: {path}")
        if path.exists():
            if not path.is_file() or path.read_bytes() != payload:
                raise CubError(f"refusing to overwrite divergent immutable output: {path}")
    for path, payload in outputs.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                    raise CubError(
                        f"refusing to overwrite divergent immutable output: {path}"
                    ) from None
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def _oracle_paths(task: str) -> list[Path]:
    task_dir = ROOT / task
    paths = [
        Path(__file__).resolve(),
        task_dir / "checker.sh",
        task_dir / "checker_data/verify.py",
    ]
    expected = task_dir / "checker_data/expected.txt"
    if expected.is_file():
        paths.append(expected)
    return paths


def _artifact_contract(task: str) -> tuple[str, str, str]:
    if task == "textedit-exact-file":
        return (
            "artifacts/openbench-exact.txt",
            "output.txt",
            "text/plain; charset=utf-8",
        )
    return (
        "artifacts/fixture-state.json",
        "state.json",
        "application/json",
    )


def _task_plan_identity(
    task: str, request_path: Path, request: Mapping[str, Any]
) -> dict[str, Any]:
    root, _repo, _installed = _request_paths(request)
    script = Path(__file__).resolve()
    verifier_command = [
        sys.executable,
        str(script),
        "--request",
        str(request_path.resolve()),
        "verify",
    ]
    oracles = _oracle_paths(task)
    verifier_digest = _content_bound_command_digest(
        verifier_command,
        cwd=_workspace(root, "installed", task, 1),
        extra_paths=oracles,
    )
    _source, artifact_name, _media = _artifact_contract(task)
    task_content = {
        "instruction": _sha256(ROOT / task / "instruction.md"),
        "verifier": verifier_digest,
        "artifacts": [f"artifacts/final-state/{artifact_name}"],
    }
    return {
        "name": f"openbench/computer-use-v0-{task}",
        "content_sha256": _canonical_digest(task_content),
    }


def _mcp_plan_identity(
    identity: Mapping[str, Any], arm: str, config_dir: Path,
    *, state_response_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "name": "computer-use-mcp",
        "version": MCP_VERSION,
        "transport": "stdio",
        "server_sha256": _content_bound_command_digest(
            [str(identity["executable"]), "serve"], cwd=config_dir
        ),
        "collector_run_id": f"cub-v0-{arm}-mcp",
        **(
            {
                "state_response_mode": state_response_mode,
                "call_contract": state_call_contract(state_response_mode),
            }
            if state_response_mode is not None
            else {}
        ),
    }


def _arm_plan_config(
    app: Mapping[str, Any], host: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "environment": {
            "platform": "macos",
            "os": {
                "version": host["os_version"],
                "build": host["os_build"],
            },
            "architecture": host["architecture"],
            "hardware_model": host["hardware"],
            "app": {
                "bundle_id": app["bundle_id"],
                "version": app["version"],
                "build": app["build"],
                "code_signature_sha256": app["signature_sha256"],
            },
            "display": {
                "width_px": host["display_width"],
                "height_px": host["display_height"],
                "scale_factor": host["display_scale"],
                "color_space": host["display_color_space"],
            },
            "preflight": {
                "accessibility": True,
                "screen_recording": True,
                "app_installed": True,
                "display_stable": True,
                "focus_monitor_ready": True,
            },
        },
    }


def _host_environment() -> dict[str, Any]:
    os_version = _run(["sw_vers", "-productVersion"], timeout=10).stdout.strip()
    os_build = _run(["sw_vers", "-buildVersion"], timeout=10).stdout.strip()
    architecture = _run(["uname", "-m"], timeout=10).stdout.strip()
    hardware = _run(["sysctl", "-n", "hw.model"], timeout=10).stdout.strip()
    displays = json.loads(
        _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=30).stdout
    )
    main_display = None
    for gpu in displays.get("SPDisplaysDataType", []):
        for display in gpu.get("spdisplays_ndrvs", []):
            if display.get("spdisplays_main") == "spdisplays_yes":
                main_display = display
                break
    if main_display is None:
        raise CubError("system_profiler did not report exactly one main display")
    resolution = re.match(r"^(\d+) x (\d+)", str(main_display.get("_spdisplays_resolution", "")))
    pixels = re.match(r"^(\d+) x (\d+)", str(main_display.get("_spdisplays_pixels", "")))
    if not resolution or not pixels:
        raise CubError("main display resolution is unavailable")
    width, height = int(resolution.group(1)), int(resolution.group(2))
    pixel_width = int(pixels.group(1))
    return {
        "os_version": os_version,
        "os_build": os_build,
        "architecture": architecture,
        "hardware": hardware,
        "display_width": width,
        "display_height": height,
        "display_scale": pixel_width / width,
        "display_color_space": str(main_display.get("_name", "unknown")),
    }


def _config_text(
    *, request_path: Path, request: Mapping[str, Any], arm: str, task: str,
    trial_index: int, trial_id: str, mcp: Mapping[str, Any],
    app: Mapping[str, Any], host: Mapping[str, Any], mode: str,
    matrix: Mapping[str, Any] | None,
) -> str:
    trial_index = _positive_trial_index(trial_index)
    root, _repo, _installed = _request_paths(request)
    workspace = _workspace(root, arm, task, trial_index)
    output, results = _result_paths(root, mode, arm, task, trial_index)
    lease = descendant(root, "runtime/native-macos.lock")
    task_dir = ROOT / task
    script = Path(__file__).resolve()
    common = [sys.executable, str(script), "--request", str(request_path)]
    setup_cmd = [*common, "setup"]
    reset_cmd = [*common, "reset"]
    verify_cmd = [*common, "verify"]
    if task in {"basic-controls", "state-response-ab"}:
        foreground = FIXTURE_BUNDLES[task]
        forbidden: list[str] = []
        tools = ["list_apps", "get_app_state", "click", "set_value", "type_text", "wait_for"]
    elif task == "background-control":
        foreground = GUARD_BUNDLE_ID
        forbidden = [FIXTURE_BUNDLES[task]]
        tools = ["list_apps", "get_app_state", "find", "set_value", "click", "click_menu_item", "wait_for"]
    else:
        foreground = FIXTURE_BUNDLES[task]
        forbidden = []
        tools = ["list_apps", "get_app_state", "click", "set_value", "type_text", "press_key", "wait_for"]
    artifact_source, artifact_name, media = _artifact_contract(task)
    forbidden_tools = sorted({
        "batch", "delete_skill", "drag", "manage_window", "open_app", "open_url", "page",
        "read_clipboard", "record_skill_start", "record_skill_stop", "run_skill", "save_skill",
        "write_clipboard",
    } - set(tools))
    oracle_paths = [str(path) for path in _oracle_paths(task)]
    state_response_mode = arm if mode == "state-ab" else None
    state_response_text = (
        f"state_response_mode = {_toml_string(state_response_mode)}\n"
        if state_response_mode is not None
        else ""
    )
    call_contract_text = ""
    if state_response_mode is not None:
        effective_contract = state_call_contract(state_response_mode)
        call_contract_text = "\n".join(
            "\n".join((
                "[[mcp.call_contract]]",
                f"tool = {_toml_string(item['tool'])}",
                "required_arguments = "
                + _toml_inline_table(item["required_arguments"]),
            ))
            for item in effective_contract
        )
    matrix_text = ""
    if matrix is not None:
        matrix_text = f'''
[matrix]
manifest = {_toml_string(str(matrix["manifest"]))}
plan = {_toml_string(str(matrix["plan"]))}
plan_sha256 = {_toml_string(str(matrix["plan_sha256"]))}
cell_id = {_toml_string(str(matrix["cell_id"]))}
cell_sha256 = {_toml_string(str(matrix["cell_sha256"]))}
config_sha256 = {_toml_string(str(matrix["config_sha256"]))}
'''
    return f'''schema_version = "openbench.native-run.v0"
trial_id = {_toml_string(trial_id)}
output_dir = {_toml_string(str(output))}
results_path = {_toml_string(str(results))}
lease_path = {_toml_string(str(lease))}
workspace = {_toml_string(str(workspace))}
atif_path = "trajectory.json"
verdict_path = "runner/verdict.json"
{matrix_text}

[task]
id = {_toml_string(f"openbench/computer-use-v0-{task}")}
instruction = {_toml_string(str(task_dir / "instruction.md"))}
verifier_oracle_paths = {_toml_array(oracle_paths)}

[harness]
name = "codex"
version = {_toml_string(str(request.get("codex_version", "codex-cli 0.146.1")))}
version_source = "native_cli"

[model]
name = "gpt-5.6-sol"
provider = "openai-codex"
revision = "gpt-5.6-sol"

[mcp]
name = "computer-use-mcp"
version = "0.4.1"
command = {_toml_array([str(mcp["executable"]), "serve"])}
client_command_env = "CUB_MCP_COMMAND"
collector_run_id = {_toml_string(f"cub-v0-{arm}-mcp")}
{state_response_text}allowed_tools = {_toml_array(tools)}
forbidden_tools = {_toml_array(forbidden_tools)}
source_revision = {_toml_string(str(mcp.get("source_revision", "installed-0.4.1")))}
binary_sha256 = {_toml_string(str(mcp["binary_sha256"]))}
app_bundle = {_toml_string(str(mcp["app"]))}
build_stamp_unix = {int(mcp["build_stamp_unix"])}
designated_requirement = {_toml_string(str(mcp["designated_requirement"]))}
{call_contract_text}

[environment]
architecture = {_toml_string(host["architecture"])}
hardware_model = {_toml_string(host["hardware"])}
mcp_bundle_id = {_toml_string(str(mcp["bundle_id"]))}

[environment.os]
version = {_toml_string(host["os_version"])}
build = {_toml_string(host["os_build"])}

[environment.app]
bundle_id = {_toml_string(str(app["bundle_id"]))}
version = {_toml_string(str(app["version"]))}
build = {_toml_string(str(app["build"]))}
code_signature_sha256 = {_toml_string(str(app["signature_sha256"]))}

[environment.display]
width_px = {int(host["display_width"])}
height_px = {int(host["display_height"])}
scale_factor = {float(host["display_scale"])}
color_space = {_toml_string(str(host["display_color_space"]))}

[budget]
timeout_s = 300
max_retries = 0

[focus]
required_foreground_bundle_id = {_toml_string(foreground)}
forbidden_bundle_ids = {_toml_array(forbidden)}
require_foreground_full_agent_phase = true
forbid_global_delivery = true
allowed_delivery_tiers = {_toml_array(DELIVERY_TIERS[task])}

[proxy]
required = true

[phases.setup]
command = {_toml_array(setup_cmd)}
timeout_s = 30

[phases.verifier]
command = {_toml_array(verify_cmd)}
timeout_s = 30

[phases.reset]
command = {_toml_array(reset_cmd)}
timeout_s = 30

[[artifacts]]
source = {_toml_string(artifact_source)}
path = {_toml_string(f"artifacts/final-state/{artifact_name}")}
media_type = {_toml_string(media)}
'''


def generate(
    request_path: Path,
    mode: str,
    arm: str | None,
    task: str | None,
    *,
    repetitions: int = 5,
    trial_index: int = 1,
) -> int:
    repetitions = _positive_trial_index(repetitions)
    trial_index = _positive_trial_index(trial_index)
    request = _load_request(request_path)
    root, _repo, installed = _request_paths(request)
    static = _static_preflight(request)
    if mode in {"matched", "state-ab"} and not static["matched_ready"]:
        failed = [item["name"] for item in static["checks"] if not item["passed"]]
        raise CubError(f"matched configs require complete identity/TCC proof; blockers: {failed}")
    if mode == "pilot" and (arm is None or task is None):
        raise CubError("pilot generation requires --arm and --task")
    if mode == "matched" and (arm is not None or task is not None):
        raise CubError("matched generation covers all arms/tasks; omit --arm and --task")
    if mode == "state-ab" and (arm is not None or task is not None):
        raise CubError("state-ab generation uses fixed auto/full arms and task")
    selected_arms = (
        STATE_RESPONSE_ARMS
        if mode == "state-ab"
        else ARMS
        if mode == "matched" or arm == "both"
        else (arm,)
    )
    selected_tasks = (
        ("state-response-ab",)
        if mode == "state-ab"
        else MATCHED_TASKS
        if mode == "matched"
        else (task,)
    )
    identities: dict[str, dict[str, Any]] = {}
    if "installed" in selected_arms:
        identities["installed"] = _bundle_info(installed)
    if "source" in selected_arms:
        source_identity = _bundle_info(root / "apps/OpenBench Computer Use MCP Source.app")
        source_identity["source_revision"] = SOURCE_REVISION
        identities["source"] = source_identity
    if mode == "state-ab":
        source_identity = _bundle_info(root / "apps/OpenBench Computer Use MCP Source.app")
        source_identity["source_revision"] = SOURCE_REVISION
        identities.update({arm_id: source_identity for arm_id in STATE_RESPONSE_ARMS})
    app_paths = {
        "basic-controls": root / "apps/ComputerUseFixture.app",
        "state-response-ab": root / "apps/ComputerUseFixture.app",
        "background-control": root / "apps/BackgroundControlFixture.app",
        "textedit-exact-file": Path("/System/Applications/TextEdit.app"),
    }
    host = _host_environment()
    app_identities = {
        selected_task: _bundle_info(app_paths[selected_task])
        for selected_task in selected_tasks
    }
    outputs: dict[Path, bytes] = {}
    cells: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    config_root = descendant(root, f"configs/{mode}")
    manifest_path = config_root / "manifest.json"
    if mode in {"matched", "state-ab"}:
        harness = {
            "name": "codex",
            "version": str(request.get("codex_version", "codex-cli 0.146.1")),
            "version_source": "native_cli",
        }
        model = {
            "name": "gpt-5.6-sol",
            "provider": "openai-codex",
            "revision": "gpt-5.6-sol",
        }
        for selected_task in selected_tasks:
            app_identity = app_identities[selected_task]
            spec = {
                "comparison_id": f"cub-v0-{selected_task}",
                "task": _task_plan_identity(
                    selected_task, request_path, request
                ),
                "harness": harness,
                "model": model,
                "arms": [
                    {
                        "id": selected_arm,
                        "mcp": _mcp_plan_identity(
                            identities[selected_arm],
                            selected_arm,
                            config_root,
                            state_response_mode=(
                                selected_arm if mode == "state-ab" else None
                            ),
                        ),
                        "config": _arm_plan_config(
                            app_identity, host
                        ),
                    }
                    for selected_arm in selected_arms
                ],
                "repetitions": repetitions,
            }
            plan = build_native_matrix(**spec)
            plan_dir = config_root / "plans"
            spec_path = plan_dir / f"{selected_task}.spec.json"
            plan_path = plan_dir / f"{selected_task}.plan.json"
            spec_bytes = canonical_bytes(spec) + b"\n"
            plan_bytes = canonical_bytes(plan) + b"\n"
            outputs[spec_path] = spec_bytes
            outputs[plan_path] = plan_bytes
            plans.append({
                "task": selected_task,
                "spec": str(spec_path),
                "spec_sha256": _bytes_sha256(spec_bytes),
                "plan": str(plan_path),
                "plan_sha256": plan["plan_sha256"],
                "plan_file_sha256": _bytes_sha256(plan_bytes),
                "plan_command": [
                    "obench", "native", "plan", str(spec_path),
                    "--output", str(plan_path),
                ],
            })
            for cell in plan["schedule"]:
                selected_arm = cell["arm_id"]
                current_trial = int(cell["block"])
                config_path = (
                    config_root / "cells" / selected_task
                    / f"trial{current_trial}-{selected_arm}.toml"
                )
                config_bytes = _config_text(
                    request_path=request_path.resolve(),
                    request=request,
                    arm=selected_arm,
                    task=selected_task,
                    trial_index=current_trial,
                    trial_id=cell["trial_id"],
                    mcp=identities[selected_arm],
                    app=app_identity,
                    host=host,
                    mode=mode,
                    matrix={
                        **cell,
                        "plan_sha256": plan["plan_sha256"],
                        "manifest": manifest_path,
                        "plan": plan_path,
                    },
                ).encode("utf-8")
                outputs[config_path] = config_bytes
                output_path, results_path = _result_paths(
                    root, mode, selected_arm, selected_task, current_trial
                )
                cells.append({
                    "task": selected_task,
                    **{
                        key: cell[key]
                        for key in (
                            "sequence", "block", "position", "arm_id",
                            "cell_id", "trial_id", "config_sha256", "cell_sha256",
                        )
                    },
                    "trial_index": current_trial,
                    "matrix_cell_key": f"{selected_task}/{cell['cell_id']}",
                    "plan_sha256": plan["plan_sha256"],
                    "config": str(config_path),
                    "runnable_config_sha256": _bytes_sha256(config_bytes),
                    "workspace": str(
                        _workspace(root, selected_arm, selected_task, current_trial)
                    ),
                    "evidence": str(
                        _evidence(root, selected_arm, selected_task, current_trial)
                    ),
                    "process_state": str(
                        _state_path(
                            root, selected_arm, selected_task, current_trial
                        )
                    ),
                    "output": str(output_path),
                    "results": str(results_path),
                    "run_command": ["obench", "native", "run", str(config_path)],
                })
    else:
        selected_task = str(task)
        app_identity = app_identities[selected_task]
        for selected_arm in selected_arms:
            selected_arm = str(selected_arm)
            trial_id = (
                f"cub-v0-pilot-{selected_arm}-{selected_task}-"
                f"trial{trial_index}"
            )
            config_path = (
                config_root / "cells" / selected_task
                / f"trial{trial_index}-{selected_arm}.toml"
            )
            config_bytes = _config_text(
                request_path=request_path.resolve(),
                request=request,
                arm=selected_arm,
                task=selected_task,
                trial_index=trial_index,
                trial_id=trial_id,
                mcp=identities[selected_arm],
                app=app_identity,
                host=host,
                mode=mode,
                matrix=None,
            ).encode("utf-8")
            outputs[config_path] = config_bytes
            output_path, results_path = _result_paths(
                root, mode, selected_arm, selected_task, trial_index
            )
            cells.append({
                "task": selected_task,
                "arm_id": selected_arm,
                "trial_index": trial_index,
                "trial_id": trial_id,
                "config": str(config_path),
                "runnable_config_sha256": _bytes_sha256(config_bytes),
                "workspace": str(
                    _workspace(
                        root,
                        selected_arm,
                        selected_task,
                        trial_index,
                    )
                ),
                "evidence": str(
                    _evidence(
                        root,
                        selected_arm,
                        selected_task,
                        trial_index,
                    )
                ),
                "process_state": str(
                    _state_path(
                        root,
                        selected_arm,
                        selected_task,
                        trial_index,
                    )
                ),
                "output": str(output_path),
                "results": str(results_path),
                "run_command": [
                    "obench",
                    "native",
                    "run",
                    str(config_path),
                ],
            })
    manifest = {
        "schema_version": "openbench.computer-use-config-set.v2",
        "mode": mode,
        "comparable": mode in {"matched", "state-ab"},
        "repetitions": repetitions if mode in {"matched", "state-ab"} else 1,
        "plans": plans,
        "cells": cells,
        "source_revision": SOURCE_REVISION,
        "basic_fixture_revision": BASIC_REVISION,
    }
    outputs[manifest_path] = canonical_bytes(manifest) + b"\n"
    _write_immutable_outputs(outputs)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("build")
    probe = sub.add_parser("probe-permissions")
    probe.add_argument("--arm", choices=ARMS, required=True)
    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument(
        "--mode", choices=("matched", "pilot", "state-ab"), required=True
    )
    generate_parser.add_argument("--arm", choices=(*ARMS, "both"))
    generate_parser.add_argument("--task", choices=TASKS)
    generate_parser.add_argument(
        "--repetitions",
        type=_positive_trial_argument,
        default=5,
        help="matched AB/BA repetition count (default: 5)",
    )
    generate_parser.add_argument(
        "--trial-index",
        type=_positive_trial_argument,
        default=1,
        help="explicit pilot trial index (default: 1)",
    )
    for name in ("setup", "reset", "verify"):
        item = sub.add_parser(name)
        item.add_argument("--arm", choices=ARMS)
        item.add_argument("--task", choices=TASKS)
        item.add_argument(
            "--trial-index",
            type=_positive_trial_argument,
        )
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return preflight(args.request)
        if args.command == "build":
            return build(args.request)
        if args.command == "probe-permissions":
            return probe_permissions(args.request, args.arm)
        if args.command == "generate":
            return generate(
                args.request,
                args.mode,
                args.arm,
                args.task,
                repetitions=args.repetitions,
                trial_index=args.trial_index,
            )
        arm, task, trial_index = _runtime_coordinates(
            args.arm, args.task, args.trial_index
        )
        if args.command == "setup":
            return setup(args.request, arm, task, trial_index)
        if args.command == "reset":
            return reset_runtime(args.request, arm, task, trial_index)
        return verify(args.request, arm, task, trial_index)
    except CubError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
