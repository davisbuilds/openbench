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
import subprocess
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY_PATH = os.path.join(HERE, "entry.py")
DOCKERFILE_DIR = os.path.join(HERE, "docker")
RESULT_SENTINEL = "__BENCH_RESULT__"
DEFAULT_IMAGE = "openbench-harness:latest"

# Extra host-side wall-clock grace beyond the adapter's own timeout before we
# hard-kill the container.
_TIMEOUT_GRACE_S = 60

# Per-harness auth surfaces, as $HOME-relative paths. Only those that exist on
# the host are mounted READ-ONLY into a staging dir; entry.py then copies them
# into the container's writable $HOME before running the adapter. This keeps the
# host's real config strictly read-only while giving CLIs that must write into
# their config home (e.g. codex's CODEX_HOME) a writable copy. $HOME is /root.
CONTAINER_HOME = "/root"
AUTH_STAGING = "/bench/auth"
AUTH_MOUNTS = {
    # codex: mount ONLY the auth/config files. ~/.codex also holds worktrees,
    # sessions, and sqlite logs (54 GB observed); mounting the whole dir made
    # entry.py's staging copy run for 13+ minutes and crash on transient
    # session tmp files vanishing mid-copy.
    "codex": [".codex/auth.json", ".codex/config.toml"],
    "pi": [".pi"],
    "opencode": [".local/share/opencode", ".config/opencode"],
    "cursor": [".cursor"],
    "devin": [".config/devin"],
    # claude runs OPEN models only (vendor keys via env, forwarded below). Mount
    # NOTHING: never expose ~/.claude so a container run can't touch the user's
    # Anthropic subscription. The API key is passed via API_KEY_PASSTHROUGH.
    "claude": [],
    "null": [],
}

# Open-model API keys forwarded into the container when set on the host.
# Passed as bare ``-e VAR`` (no value) so docker reads them from the client's
# environment and the secret never appears in argv or logged commands.
API_KEY_PASSTHROUGH = ("ZAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY")


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


def _auth_mount_args(harness):
    """Read-only ``-v`` args mounting the harness's auth into the staging dir.

    entry.py copies these into the writable ``$HOME`` at container start, so the
    host's real config is never mounted writable.
    """
    args = []
    for rel in AUTH_MOUNTS.get(harness, []):
        host_path = os.path.join(os.path.expanduser("~"), rel)
        if os.path.exists(host_path):
            staged_path = f"{AUTH_STAGING}/{rel}"
            args += ["-v", f"{host_path}:{staged_path}:ro"]
    return args


def build_docker_cmd(harness, workdir, model, timeout_s, adapters_dir, image,
                     instruction_path, container_name=None,
                     extra_docker_args=None):
    """Assemble the ``docker run`` argv for one cell (pure; unit-testable)."""
    cmd = ["docker", "run", "--rm"]
    if container_name:
        cmd += ["--name", container_name]
    cmd += [
        "-v", f"{os.path.abspath(workdir)}:/work",
        "-v", f"{os.path.abspath(adapters_dir)}:/bench/adapters:ro",
        "-v", f"{ENTRY_PATH}:/bench/entry.py:ro",
        "-v", f"{os.path.abspath(instruction_path)}:/bench/instruction.txt:ro",
        "-w", "/work",
        "-e", f"HOME={CONTAINER_HOME}",
    ]
    for var in API_KEY_PASSTHROUGH:
        if os.environ.get(var):
            cmd += ["-e", var]
    cmd += _auth_mount_args(harness)
    if extra_docker_args:
        cmd += list(extra_docker_args)
    cmd += [
        image,
        "python3", "/bench/entry.py", harness, model, str(timeout_s),
    ]
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


def run_in_container(harness, instruction, workdir, model, timeout_s,
                     adapters_dir, image=DEFAULT_IMAGE, extra_docker_args=None):
    """Run one cell in a container and return the adapter result dict.

    Raises ``DockerUnavailable`` (caller falls back to local) when the daemon or
    image is missing. Container crashes / missing result are returned as a
    failed result dict (``completed=False``), never raised, so the runner loop
    keeps going.
    """
    preflight(image)

    # Instruction goes through a mounted temp file (avoids env/arg size and
    # quoting issues with multi-line task instructions).
    fd, instruction_path = tempfile.mkstemp(prefix="bench_instr_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(instruction)

        # Unique name so a hung container (an inner CLI that never exits and
        # holds the stdout pipe open) can be force-killed on timeout. Killing
        # the `docker run` client alone does NOT stop the container.
        container_name = f"openbench_{harness}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        cmd = build_docker_cmd(
            harness, workdir, model, timeout_s, adapters_dir, image,
            instruction_path, container_name=container_name,
            extra_docker_args=extra_docker_args,
        )

        # The container must not outlive this call on ANY exit path — timeout,
        # crash, Ctrl-C, or a clean return where `--rm` glitched. Killing the
        # `docker run` client alone does not stop the container, so removal by
        # name is guaranteed in the finally (a no-op when `--rm` already
        # cleaned up).
        timeout_result = None
        cleanup_ok = True
        try:
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout_s + _TIMEOUT_GRACE_S,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                # TimeoutExpired can carry bytes even under text=True.
                def _text(x):
                    if isinstance(x, bytes):
                        return x.decode("utf-8", errors="replace")
                    return x or ""
                full_output = _text(e.stdout) + _text(e.stderr)
                timeout_result = {
                    "completed": False,
                    "error": f"container timeout after {timeout_s}s (+grace); killed",
                    "output_tail": full_output[-2000:],
                    "full_output": full_output,
                    "tokens": None, "turns": None, "cmd": cmd,
                }
        finally:
            cleanup_ok = _force_remove_container(container_name)
        if not cleanup_ok:
            tail = (timeout_result or {}).get("output_tail", "")
            return {
                "completed": False,
                "error": f"container cleanup failed for {container_name}; "
                         "container may still be running",
                "output_tail": tail,
                "full_output": (timeout_result or {}).get("full_output", tail),
                "tokens": None, "turns": None, "cmd": cmd,
            }
        if timeout_result is not None:
            return timeout_result

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
            }
        # Record the docker invocation for the results log (adapters record the
        # inner CLI cmd; we prepend the container wrapper for provenance).
        result["cmd"] = {"docker": cmd, "adapter_cmd": result.get("cmd")}
        return result
    finally:
        os.unlink(instruction_path)
