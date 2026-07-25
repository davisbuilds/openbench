#!/usr/bin/env python3
"""Container-per-run execution backend for the benchmark runner.

``--exec docker`` routes each cell through a fresh, disposable container so the
agent's file edits and shell commands are isolated from the host. The SAME
adapter module runs unchanged inside the container (see ``entry.py``); this
module only builds/launches the container and marshals the result dict back.

Design:
  - One ``docker run --rm`` per cell (container-per-run).
  - Bind mounts:
      <workdir>            -> /work            (read-write; agent edits land here)
      <adapters_dir>       -> /bench/adapters  (read-only)
      <this_dir>/entry.py  -> /bench/entry.py  (read-only)
      <temp instruction>   -> /bench/instruction.txt (read-only)
      <per-harness auth>   -> under $HOME       (read-only; never baked in image)
  - Auth is MOUNTED at runtime, read-only. Secrets are never copied into the
    image, so the image is safe to share/rebuild.
  - Host-side subprocess timeout = timeout_s + GRACE kills a hung container;
    the adapter also enforces timeout_s internally.
  - ``DockerUnavailable`` is raised when the daemon or image is missing so the
    runner can fall back to local execution.

stdlib only.
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from types import SimpleNamespace

from .auth_persist import AUTH_PERSIST, try_persist_auth_file
from .paths import PACKAGE_DIR, SOURCE_ROOT

HERE = PACKAGE_DIR
REPO_ROOT = SOURCE_ROOT
ENTRY_PATH = os.path.join(HERE, "entry.py")
CANDIDATES_PATH = os.path.join(HERE, "candidates.py")
AUTH_PERSIST_PATH = os.path.join(HERE, "auth_persist.py")
GATEWAY_SPEC_PATH = os.path.join(HERE, "gateway_spec.py")
GATEWAY_PROFILES_PATH = os.path.join(HERE, "gateway_profiles.py")
DOCKERFILE_DIR = os.path.join(HERE, "docker")
RESULT_SENTINEL = "__BENCH_RESULT__"
DEFAULT_IMAGE = "openbench-harness:latest"

# Extra host-side wall-clock grace beyond the adapter's own timeout before we
# hard-kill the container.
_TIMEOUT_GRACE_S = 60
_DOCKER_CLIENT_POLL_INTERVAL_S = 0.2
_DOCKER_CLIENT_KILL_GRACE_S = 5

# Per-harness auth surfaces, as $HOME-relative paths. Only those that exist on
# the host are mounted READ-ONLY into a staging dir; entry.py then copies them
# into the container's writable $HOME before running the adapter. This keeps the
# host's real config strictly read-only while giving CLIs that must write into
# their config home (e.g. codex's CODEX_HOME) a writable copy. $HOME is /root.
CONTAINER_HOME = "/root"
AUTH_STAGING = "/bench/auth"
AUTH_RETURN = "/bench/auth-return"

AUTH_MOUNTS = {
    # codex: mount ONLY the auth/config files. ~/.codex also holds worktrees,
    # sessions, and sqlite logs (54 GB observed); mounting the whole dir made
    # entry.py's staging copy run for 13+ minutes and crash on transient
    # session tmp files vanishing mid-copy. codex_v1/v2 compose a fresh runtime
    # CODEX_HOME in the adapter and reuse this same staged auth surface.
    "codex": [".codex/auth.json"],
    "codex_v1": [".codex/auth.json"],
    "codex_v2": [".codex/auth.json"],
    "pi": [".pi/agent/auth.json"],
    "opencode": [".local/share/opencode/auth.json", ".opencode/data/auth.json"],
    # Cursor Linux/container auth: `bench/cursor_container_login.sh` mints auth
    # under ~/.openbench/cursor-container-auth, laid out as a HOME subtree. Map
    # those host paths back to Linux cursor-agent's HOME paths in the container;
    # legacy ~/.cursor remains a fallback if no container-auth .cursor exists.
    "cursor": [
        (".openbench/cursor-container-auth/.config/cursor/auth.json", ".config/cursor/auth.json"),
    ],
    # devin keeps its config in ~/.config/devin and its login token in
    # ~/.local/share/devin/credentials.toml (v3000.x layout); stage both so the
    # container CLI is actually logged in.
    "devin": [
        ".config/devin",
        (".local/share/devin/credentials.toml", ".local/share/devin/credentials.toml"),
    ],
    # claude uses API keys only (open-model vendor keys or first-party
    # ANTHROPIC_API_KEY, forwarded below). Mount NOTHING: never expose ~/.claude
    # so a container run can't touch the user's Claude Code OAuth subscription.
    "claude": [],
    # grokbuild's BYOK/CLIProxyAPI lanes need no ~/.grok auth, but the native
    # xAI subscription lane (grok-4.5) stages ~/.grok/auth.json into its
    # disposable HOME, so the container needs the login file present.
    "grokbuild": [
        # Container-native login (grok's Linux build binds auth to its own OIDC
        # client, so host macOS credentials do not port). Minted once via
        # `grok login --device-code` in the image; falls back to host ~/.grok
        # for hosts whose auth was minted on Linux.
        (".openbench/grok-container-auth/auth.json", ".grok/auth.json"),
        (".grok/auth.json", ".grok/auth.json"),
    ],
    "null": [],
}

# Model API keys forwarded into the container when set on the host. Passed as
# bare ``-e VAR`` (no value), so secrets never appear in argv/logged commands.
API_KEY_PASSTHROUGH = ("ZAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "ANTHROPIC_API_KEY", "CURSOR_API_KEY", "CLIPROXYAPI_API_KEY", "OPENROUTER_API_KEY")

_MODEL_API_KEY = {
    "glm-5.2": "ZAI_API_KEY",
    "glm-4.7-flash": "ZAI_API_KEY",
    "deepseek-v4-flash": "DEEPSEEK_API_KEY",
    "kimi-k2.7-code": "MOONSHOT_API_KEY",
    "kimi-k3": "MOONSHOT_API_KEY",
    "gpt-5.6": "CLIPROXYAPI_API_KEY",
    "gpt-5.6-sol": "CLIPROXYAPI_API_KEY",
    "laguna-s-2.1": "OPENROUTER_API_KEY",
    "inkling": "OPENROUTER_API_KEY",
}
_KEYS_ENV = os.path.expanduser("~/.openbench/keys.env")


def _keys_env_has(var):
    """True when ~/.openbench/keys.env defines var (without exposing value)."""
    try:
        with open(_KEYS_ENV, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                try:
                    parts = shlex.split(line, comments=True, posix=True)
                    first = parts[1] if parts and parts[0] == "export" and len(parts) > 1 else parts[0]
                    key, val = first.split("=", 1)
                except (ValueError, IndexError):
                    key, val = line.split("=", 1)[0].strip(), line.split("=", 1)[1].strip()
                if key == var and val.strip():
                    return True
    except OSError:
        return False
    return False


def _host_has_key(var):
    return bool(os.environ.get(var) or _keys_env_has(var))


def _api_key_passthrough(harness, model):
    """Secret env vars needed by this exact docker cell.

    Keep pass-through scoped: agents can run shell tools, so forwarding Cursor or
    Anthropic keys into unrelated harness containers would expose credentials in
    the wrong trust boundary. Codex bridge runs get placeholders instead; the
    real upstream keys stay only in the host-side LiteLLM bridge process.
    """
    needed = set()
    if harness == "cursor":
        needed.add("CURSOR_API_KEY")
    if model == "claude-opus-4-8" and harness == "claude":
        needed.add("ANTHROPIC_API_KEY")
    vendor_key = _MODEL_API_KEY.get(model)
    if vendor_key and harness in {"pi", "opencode", "claude", "grokbuild"}:
        # CLIProxyAPI ingress auth is optional; grokbuild supplies a local
        # placeholder when this variable is absent.
        needed.add(vendor_key)
    return tuple(var for var in API_KEY_PASSTHROUGH if var in needed)


def _placeholder_env(harness, model):
    """Non-secret env assignments needed by CLIs for bridge ingress only."""
    if harness not in {"codex", "codex_v1", "codex_v2"}:
        return ()
    if model == "claude-opus-4-8":
        var = "ANTHROPIC_API_KEY"
    else:
        var = _MODEL_API_KEY.get(model)
    return (f"{var}=openbench-bridge-placeholder",) if var and _host_has_key(var) else ()


class DockerUnavailable(Exception):
    """Raised when the daemon/image is unavailable and the caller should fall back."""


def daemon_running():
    """Return True if the Docker daemon is reachable."""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def image_exists(image):
    """Return True if a local image with this tag exists."""
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _split_image(image):
    """Return (repository, tag) for a Docker image ref used by this runner."""
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        return image.rsplit(":", 1)
    return image, "latest"


def _retag_corrupt_image(image):
    """Repair a corrupted tag when `docker images` can still see its image ID.

    Observed failure: `docker image inspect openbench-harness:latest` says the
    tag is missing/corrupt, while `docker images` still lists the repository/tag
    with an image ID. Re-tagging that ID restores inspect/run. Returns True when
    a re-tag was attempted and inspect succeeds afterwards.
    """
    repo, tag = _split_image(image)
    try:
        proc = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}\t{{.Tag}}\t{{.ID}}"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    image_id = None
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == repo and parts[1] == tag and parts[2]:
            image_id = parts[2]
            break
    if not image_id:
        return False
    print(f"WARN docker image tag {image!r} failed inspect; re-tagging {image_id} as {image}")
    try:
        tag_proc = subprocess.run(
            ["docker", "tag", image_id, image],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return tag_proc.returncode == 0 and image_exists(image)


def preflight(image, retries=3, delay_s=5):
    """Raise ``DockerUnavailable`` with a specific reason if we can't run in docker.

    Retries a few times before giving up: Docker Desktop's resource saver
    pauses the VM between cells, and the first probe after a pause can fail
    transiently even though the daemon and image are fine (observed 2026-07-05:
    a 15-cell segment burned every cell in ~7s on a paused engine).
    """
    for attempt in range(retries):
        daemon_ok = daemon_running()
        if daemon_ok and (image_exists(image) or _retag_corrupt_image(image)):
            return
        if attempt < retries - 1:
            time.sleep(delay_s)
    if not daemon_running():
        raise DockerUnavailable(
            "docker daemon not reachable (is Docker Desktop running?)")
    raise DockerUnavailable(
        f"image {image!r} not found (build it: "
        f"docker build -t {image} {DOCKERFILE_DIR})")


def force_remove_container(name, attempts=3, delay_s=2):
    """Public alias for _force_remove_container. Force-remove a named container."""
    return _force_remove_container(name, attempts=attempts, delay_s=delay_s)


def _force_remove_container(name, attempts=3, delay_s=2):
    """Force-remove a container and VERIFY it is gone; retry if not.

    A wedged inner CLI (holding the container's stdout pipe past the adapter
    timeout) has been observed to survive a single ``docker rm -f`` on a busy
    daemon, so removal is verified with ``docker ps -a`` and retried. Returns
    True once the container no longer exists (including "was never created").
    """
    for attempt in range(attempts):
        try:
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, text=True, timeout=30)
            probe = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                capture_output=True, text=True, timeout=20,
            )
            if probe.returncode == 0 and not probe.stdout.strip():
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if attempt < attempts - 1:
            time.sleep(delay_s)
    return False


def _read_tempfile_text(fh):
    """Return all bytes currently captured in a temp file as replacement text."""
    fh.flush()
    fh.seek(0)
    return fh.read().decode("utf-8", errors="replace")


def _stop_process(proc, terminate_grace_s=_DOCKER_CLIENT_KILL_GRACE_S):
    """Best-effort stop for a still-running docker client process."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=terminate_grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=terminate_grace_s)
    except subprocess.TimeoutExpired:
        pass


def _run_docker_client_with_deadline(cmd, container_name, host_timeout_s,
                                     poll_interval_s=_DOCKER_CLIENT_POLL_INTERVAL_S):
    """Run ``docker run`` without trusting ``Popen.communicate(timeout=...)``.

    ``subprocess.run(..., capture_output=True, timeout=...)`` enforces its
    deadline from inside ``communicate()``, while the observed wedge had the
    runner parked in ``select.poll`` there for far longer than the requested
    timeout. This helper avoids that path entirely: stdout/stderr are redirected
    to temp files, and a simple host loop polls the docker client while checking
    both a monotonic deadline and a real wall-clock deadline. On expiry it first
    force-removes the named container (the only reliable way to stop a live
    ``docker run`` container), then terminates/kills the client if needed.
    """
    proc = None
    timed_out = False
    start_monotonic = time.monotonic()
    start_wall = time.time()
    with tempfile.TemporaryFile() as stdout_fh, tempfile.TemporaryFile() as stderr_fh:
        try:
            proc = subprocess.Popen(
                cmd, stdout=stdout_fh, stderr=stderr_fh,
                stdin=subprocess.DEVNULL,
            )
            monotonic_deadline = start_monotonic + host_timeout_s
            wall_deadline = start_wall + host_timeout_s
            while proc.poll() is None:
                monotonic_remaining = monotonic_deadline - time.monotonic()
                wall_remaining = wall_deadline - time.time()
                remaining = min(monotonic_remaining, wall_remaining)
                if remaining <= 0:
                    timed_out = True
                    break
                time.sleep(min(poll_interval_s, remaining))

            if timed_out:
                _force_remove_container(container_name)
                _stop_process(proc)
        except BaseException:
            if proc is not None and proc.poll() is None:
                _force_remove_container(container_name)
                _stop_process(proc)
            raise

        elapsed_s = max(time.monotonic() - start_monotonic,
                        time.time() - start_wall)
        return SimpleNamespace(
            returncode=proc.returncode,
            stdout=_read_tempfile_text(stdout_fh),
            stderr=_read_tempfile_text(stderr_fh),
            timed_out=timed_out,
            host_wall_time_s=elapsed_s,
        )


def _auth_persist_targets(harness):
    """Return existing (host master, container-HOME relative) auth targets."""
    targets = []
    staged = set()
    home = os.path.expanduser("~")
    for host_rel, dest_rel in AUTH_PERSIST.get(harness, []):
        host_path = os.path.join(home, host_rel)
        if dest_rel not in staged and os.path.isfile(host_path):
            targets.append((host_path, dest_rel))
            staged.add(dest_rel)
    return targets


def _candidate_auth_persist_targets(auth_files):
    """Persist targets from a candidate's declared auth_files (opt-in path)."""
    targets = []
    staged = set()
    for auth in auth_files or []:
        dest_rel = auth.get("destination", "")
        source = auth.get("source", "")
        if not dest_rel or dest_rel in staged:
            continue
        host_path = os.path.expanduser(source)
        if os.path.isfile(host_path):
            targets.append((host_path, dest_rel))
            staged.add(dest_rel)
    return targets


def _persist_returned_auth(return_dir, targets):
    for master, relative in targets:
        try_persist_auth_file(os.path.join(return_dir, relative), master)


def _auth_mount_args(harness):
    """Read-only ``-v`` args mounting the harness's auth into the staging dir.

    entry.py copies these into the writable ``$HOME`` at container start, so the
    host's real config is never mounted writable.
    """
    args = []
    staged = set()
    for item in AUTH_MOUNTS.get(harness, []):
        if isinstance(item, tuple):
            host_rel, dest_rel = item
        else:
            host_rel = dest_rel = item
        host_path = os.path.join(os.path.expanduser("~"), host_rel)
        staged_path = f"{AUTH_STAGING}/{dest_rel}"
        if staged_path in staged:
            continue
        if os.path.exists(host_path):
            staged.add(staged_path)
            args += ["-v", f"{host_path}:{staged_path}:ro"]
    return args


def build_docker_cmd(harness, workdir, model, timeout_s, adapters_dir, image,
                     instruction_path, container_name=None,
                     extra_docker_args=None, extra_env=None,
                     candidate_path=None, base_harness=None,
                     candidate_auth_files=None, candidate_pass_env=None,
                     candidate_config_dir=None, candidate_inherit_env=False,
                     auth_return_dir=None, candidate_persist_auth=False, container_workdir="/work"):
    """Assemble the ``docker run`` argv for one cell (pure; unit-testable)."""
    cmd = ["docker", "run", "--rm"]
    # Bound each cell's CPU quota so co-tenant host load cannot starve a cell
    # (and cells cannot starve each other). Matches the determinism-cert config.
    cell_cpus = os.environ.get("OPENBENCH_CELL_CPUS", "4")
    if cell_cpus and cell_cpus != "0":
        cmd += ["--cpus", cell_cpus]
    if container_name:
        cmd += ["--name", container_name]
    # Candidate names are display labels, never authority to inherit a stock
    # adapter's credentials. Config variants pass their trusted base explicitly;
    # generic manifests intentionally resolve to no stock harness here.
    effective_harness = base_harness if candidate_path else harness
    cmd += [
        "-v", f"{os.path.abspath(workdir)}:{container_workdir}",
        "-v", f"{os.path.abspath(adapters_dir)}:/bench/adapters:ro",
        "-v", f"{ENTRY_PATH}:/bench/entry.py:ro",
        "-v", f"{AUTH_PERSIST_PATH}:/bench/auth_persist.py:ro",
        "-v", f"{GATEWAY_SPEC_PATH}:/bench/gateway_spec.py:ro",
        "-v", f"{GATEWAY_PROFILES_PATH}:/bench/gateway_profiles.py:ro",
        "-v", f"{os.path.abspath(instruction_path)}:/bench/instruction.txt:ro",
    ]
    candidate_arg = None
    if candidate_path:
        candidate_arg = "/bench/candidate.toml"
        cmd += [
            "-v", f"{os.path.abspath(candidate_path)}:{candidate_arg}:ro",
            "-v", f"{CANDIDATES_PATH}:/bench/candidates.py:ro",
        ]
        if candidate_config_dir:
            cmd += [
                "-v", f"{os.path.abspath(candidate_config_dir)}:/bench/candidate-config:ro",
                "-e", "OPENBENCH_CANDIDATE_CONFIG_DIR=/bench/candidate-config",
            ]
    if candidate_path is None and harness in {"codex_v1", "codex_v2"}:
        variant = harness.replace("codex_", "")
        host_variant = os.path.join(REPO_ROOT, "ablation", f"codex-home-{variant}")
        container_variant = f"/bench/ablation/codex-home-{variant}"
        cmd += ["-v", f"{host_variant}:{container_variant}:ro"]
    cmd += [
        "-w", container_workdir,
        "-e", f"BENCH_WORKDIR={container_workdir}",
        "-e", f"HOME={CONTAINER_HOME}",
    ]
    for assignment in _placeholder_env(effective_harness, model):
        cmd += ["-e", assignment]
    for var in _api_key_passthrough(effective_harness, model):
        if os.environ.get(var):
            cmd += ["-e", var]
    # Preserve an operator-selected bridge address inside Grok containers.  The
    # adapter rewrites loopback values to host.docker.internal; non-loopback
    # HTTPS endpoints pass through unchanged.  Keep the value out of argv.
    if (effective_harness == "grokbuild"
            and model in {"gpt-5.6", "gpt-5.6-sol"}
            and os.environ.get("CLIPROXYAPI_BASE_URL")):
        cmd += ["-e", "CLIPROXYAPI_BASE_URL"]
    for key, value in (extra_env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    pass_names = os.environ if candidate_inherit_env else (candidate_pass_env or [])
    runner_owned = {"HOME", *(extra_env or {}).keys()}
    for name in pass_names:
        # The container HOME and per-cell proxy values are runner-owned. A host
        # variable with the same name must not override their explicit values.
        if name not in runner_owned and name in os.environ:
            cmd += ["-e", name]
    stock_auth_args = _auth_mount_args(effective_harness)
    cmd += stock_auth_args
    stock_persist = effective_harness in AUTH_PERSIST
    if auth_return_dir and (stock_persist or candidate_persist_auth):
        cmd += [
            "-v", f"{os.path.abspath(auth_return_dir)}:{AUTH_RETURN}:rw",
        ]
        if stock_persist:
            cmd += ["-e", f"BENCH_AUTH_PERSIST_HARNESS={effective_harness}"]
        if candidate_persist_auth:
            cmd += ["-e", "BENCH_AUTH_PERSIST_CANDIDATE=1"]
    mounted_auth_targets = {
        stock_auth_args[i + 1].rsplit(":ro", 1)[0].rsplit(":", 1)[-1]
        for i, item in enumerate(stock_auth_args[:-1]) if item == "-v"
    }
    # Arbitrary manifests can declare auth paths that have no stock adapter
    # registry entry. Mount home-relative sources read-only at the same staged
    # path; entry.py copies them into the writable container HOME.
    home = os.path.realpath(os.path.expanduser("~"))
    for auth in candidate_auth_files or []:
        source = os.path.realpath(os.path.expanduser(auth["source"]))
        try:
            relative = os.path.relpath(source, home)
        except ValueError:
            continue
        if relative == ".." or relative.startswith(".." + os.sep):
            raise ValueError("Docker candidate auth sources must be under the user's home")
        target = f"{AUTH_STAGING}/{relative}"
        if os.path.isfile(source) and target not in mounted_auth_targets:
            cmd += ["-v", f"{source}:{target}:ro"]
            mounted_auth_targets.add(target)
    if extra_docker_args:
        cmd += list(extra_docker_args)
    cmd += [image, "python3", "/bench/entry.py", harness, model, str(timeout_s)]
    if candidate_arg:
        cmd.append(candidate_arg)
    return cmd


def _parse_result(stdout):
    """Extract the sentinel-tagged result dict from container stdout."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_SENTINEL):
            payload = line[len(RESULT_SENTINEL):].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                break
    return None


def image_digest(image):
    """Return RepoDigest or image ID for an already-preflighted docker image."""
    for fmt in ("{{index .RepoDigests 0}}", "{{.Id}}"):
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", "--format", fmt, image],
                capture_output=True, text=True, timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        value = (proc.stdout or "").strip()
        if proc.returncode == 0 and value and value != "<no value>":
            return value
    return None


def run_in_container(harness, instruction, workdir, model, timeout_s,
                     adapters_dir, image=DEFAULT_IMAGE, extra_docker_args=None,
                     extra_env=None, candidate_path=None, base_harness=None,
                     candidate_auth_files=None, candidate_pass_env=None,
                     candidate_config_dir=None, candidate_inherit_env=False,
                     candidate_spec_bytes=None, candidate_config_contents=None,
                     candidate_persist_auth=False, container_workdir="/work"):
    """Run one cell in a container and return the adapter result dict.

    Raises ``DockerUnavailable`` (caller falls back to local) when the daemon or
    image is missing. Container crashes / missing result are returned as a
    failed result dict (``completed=False``), never raised, so the runner loop
    keeps going.
    """
    env_setup_start = time.monotonic()
    instruction_path = None
    candidate_spec_path = None
    candidate_config_stage = None
    auth_return_dir = None
    auth_persist_targets = []
    agent_started = False
    try:
        preflight(image)
        resolved_image = image_digest(image)
        image_for_run = resolved_image or image

        # Instruction goes through a mounted temp file (avoids env/arg size and
        # quoting issues with multi-line task instructions). The file must live in
        # a directory the docker VM can bind-mount: on colima the default macOS
        # /var/folders temp path is NOT shared into the VM, so default to a
        # repo-local dir (override with OPENBENCH_DOCKER_TMPDIR).
        instr_dir = os.environ.get("OPENBENCH_DOCKER_TMPDIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".bench-tmp")
        os.makedirs(instr_dir, exist_ok=True)
        if candidate_spec_bytes is not None:
            fd, candidate_spec_path = tempfile.mkstemp(
                prefix="bench_candidate_", suffix=".toml", dir=instr_dir)
            with os.fdopen(fd, "wb") as fh:
                fh.write(candidate_spec_bytes)
        if candidate_config_contents is not None:
            candidate_config_stage = tempfile.mkdtemp(
                prefix="bench_candidate_config_", dir=instr_dir)
            for source, content in candidate_config_contents.items():
                dst = os.path.join(candidate_config_stage, source)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(content)
        fd, instruction_path = tempfile.mkstemp(prefix="bench_instr_", suffix=".txt", dir=instr_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(instruction)
        effective_harness = base_harness if (candidate_path or candidate_spec_bytes is not None) else harness
        auth_persist_targets = _auth_persist_targets(effective_harness)
        if candidate_persist_auth:
            seen = {dest for _, dest in auth_persist_targets}
            for master, dest in _candidate_auth_persist_targets(candidate_auth_files):
                if dest not in seen:
                    auth_persist_targets.append((master, dest))
                    seen.add(dest)
        if auth_persist_targets:
            auth_return_dir = tempfile.mkdtemp(prefix="bench_auth_return_", dir=instr_dir)
            os.chmod(auth_return_dir, 0o700)

        # Unique name so a hung container (an inner CLI that never exits and
        # holds the stdout pipe open) can be force-killed on timeout. Killing
        # the `docker run` client alone does NOT stop the container.
        container_name = f"openbench_{harness}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        cmd = build_docker_cmd(
            harness, workdir, model, timeout_s, adapters_dir, image_for_run,
            instruction_path, container_name=container_name,
            extra_docker_args=extra_docker_args, extra_env=extra_env,
            candidate_path=candidate_spec_path or candidate_path, base_harness=base_harness,
            candidate_auth_files=candidate_auth_files,
            candidate_pass_env=candidate_pass_env,
            candidate_config_dir=candidate_config_stage,
            candidate_inherit_env=candidate_inherit_env,
            auth_return_dir=auth_return_dir,
            candidate_persist_auth=candidate_persist_auth,
            container_workdir=container_workdir,
        )
        host_env_setup_s = round(time.monotonic() - env_setup_start, 3)

        # The container must not outlive this call on ANY exit path — timeout,
        # crash, Ctrl-C, or a clean return where `--rm` glitched. Killing the
        # `docker run` client alone does not stop the container, so removal by
        # name is guaranteed in the finally (a no-op when `--rm` already
        # cleaned up).
        cleanup_ok = True
        try:
            agent_started = True
            proc = _run_docker_client_with_deadline(
                cmd, container_name, timeout_s + _TIMEOUT_GRACE_S,
            )
        finally:
            cleanup_ok = _force_remove_container(container_name)
        # The final verified sweep is authoritative: a watchdog-timeout removal
        # can fail transiently and still be recovered here.
        if not cleanup_ok:
            full_output = (proc.stdout or "") + (proc.stderr or "") if "proc" in locals() else ""
            agent_wall = proc.host_wall_time_s if "proc" in locals() else None
            return {
                "completed": False,
                "error": f"container cleanup failed for {container_name}; "
                         "container may still be running",
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None, "turns": None, "cmd": cmd,
                "host_wall_time_s": agent_wall,
                "host_env_setup_s": host_env_setup_s,
                "host_agent_wall_time_s": agent_wall,
                "image_digest": resolved_image,
            }
        if proc.timed_out:
            full_output = (proc.stdout or "") + (proc.stderr or "")
            return {
                "completed": False,
                "error": f"container timeout after {timeout_s}s (+grace); killed",
                "output_tail": full_output[-2000:],
                "full_output": full_output,
                "tokens": None, "turns": None, "cmd": cmd,
                "host_wall_time_s": proc.host_wall_time_s,
                "host_env_setup_s": host_env_setup_s,
                "host_agent_wall_time_s": proc.host_wall_time_s,
                "image_digest": resolved_image,
            }

        combined = (proc.stdout or "") + (proc.stderr or "")
        result = _parse_result(proc.stdout or "")
        if result is None:
            return {
                "completed": False,
                "error": f"container produced no result sentinel "
                         f"(exit {proc.returncode})",
                "output_tail": combined[-2000:],
                "full_output": combined,
                "tokens": None, "turns": None, "cmd": cmd,
                "host_wall_time_s": proc.host_wall_time_s,
                "host_env_setup_s": host_env_setup_s,
                "host_agent_wall_time_s": proc.host_wall_time_s,
                "image_digest": resolved_image,
            }
        # Record the docker invocation for the results log (adapters record the
        # inner CLI cmd; we prepend the container wrapper for provenance).
        result["cmd"] = {"docker": cmd, "adapter_cmd": result.get("cmd")}
        result["host_wall_time_s"] = proc.host_wall_time_s
        result["host_env_setup_s"] = host_env_setup_s
        result["host_agent_wall_time_s"] = proc.host_wall_time_s
        result["image_digest"] = resolved_image
        return result
    except BaseException as exc:
        if not agent_started:
            setattr(exc, "bench_env_setup_s", round(time.monotonic() - env_setup_start, 3))
            setattr(exc, "bench_agent_wall_time_s", 0.0)
        raise
    finally:
        if auth_return_dir is not None:
            try:
                _persist_returned_auth(auth_return_dir, auth_persist_targets)
            finally:
                shutil.rmtree(auth_return_dir, ignore_errors=True)
        if instruction_path is not None:
            os.unlink(instruction_path)
        if candidate_spec_path is not None:
            os.unlink(candidate_spec_path)
        if candidate_config_stage:
            shutil.rmtree(candidate_config_stage, ignore_errors=True)
