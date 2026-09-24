"""Optional Harbor Docker environment for isolated repair trials.

Harbor imports are lazy. The scheduler and execution lifecycle remain Harbor's;
this provider narrows Docker launch, file transfer, and model access authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import uuid


UID = 10001
SEAL_TIMEOUT_SECONDS = 60
RELAY_SOCKET = "/run/openbench-model/gateway.sock"
RELAY_PORT = 8765
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_LOG_FILE_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 48 * 1024 * 1024
MAX_TRANSFER_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096


class SandboxError(RuntimeError):
    pass


class SandboxArtifactError(SandboxError):
    """Candidate output violates the source contract after confirmed shutdown."""
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = dict(receipt)


class TransferLimitError(SandboxError):
    def __init__(self, message, *, observed=None, limit=None):
        super().__init__(message)
        self.violation = {"kind": "transfer_bytes", "observed_at_least": observed, "limit": limit}


class ArchiveLimitError(SandboxError):
    def __init__(self, message, *, kind, observed, limit, path=None):
        super().__init__(message)
        self.violation = {"kind": kind, "observed": observed, "limit": limit}
        if path is not None:
            self.violation["path"] = path[:1024]


def validate_image(image: str) -> str:
    if not isinstance(image, str) or not re.fullmatch(
        r"(?:[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@)?sha256:[0-9a-f]{64}", image
    ):
        raise SandboxError("runtime_image must be an immutable image digest")
    return image


def relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise SandboxError("invalid artifact path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in raw.split("/")):
        raise SandboxError("artifact path must be normalized and relative")
    return str(path)


def read_regular(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read a bounded regular single-link file without following its final link."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise SandboxError("expected a bounded regular single-link file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if len(data) > limit or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SandboxError("file changed or exceeded its byte limit")
        return data
    finally:
        os.close(fd)


def read_tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("workspace must be a real directory")
    files = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxError("workspace contains a link or special file")
        name = relative_path(path.relative_to(root).as_posix())
        files[name] = read_regular(path)
        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_SOURCE_BYTES:
            raise SandboxError("workspace exceeds its bound")
    if not files:
        raise SandboxError("workspace is empty")
    return files


def pack_files(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, data in sorted(files.items()):
            item = tarfile.TarInfo(relative_path(name))
            item.size, item.mode, item.uid, item.gid = len(data), 0o644, UID, UID
            archive.addfile(item, io.BytesIO(data))
    return out.getvalue()


def unpack_files(data: bytes, *, root: str = ".", allowed: set[str] | None = None,
                 total_limit: int = MAX_SOURCE_BYTES,
                 file_limit: int = MAX_FILE_BYTES) -> dict[str, bytes]:
    """Parse Docker's tar without extracting or trusting any archive metadata."""
    if len(data) > MAX_TRANSFER_BYTES:
        raise SandboxError("archive exceeds transfer bound")
    found = {}
    seen = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for index, entry in enumerate(archive):
                if index >= MAX_FILES:
                    raise SandboxError("archive has too many entries")
                name = entry.name
                if name in (".", "./", root, root + "/") and entry.isdir():
                    continue
                if name.startswith("./"):
                    name = name[2:]
                if root != "." and name.startswith(root + "/"):
                    name = name[len(root) + 1:]
                name = relative_path(name.rstrip("/") if entry.isdir() else name)
                if name in seen:
                    raise SandboxError("duplicate archive path")
                seen.add(name)
                if entry.isdir():
                    continue
                if not entry.isreg() or entry.issparse() or entry.linkname:
                    raise SandboxError("archive contains links or special files")
                if entry.size < 0 or entry.size > file_limit:
                    raise ArchiveLimitError("archive file exceeds bound", kind="file_bytes",
                                            path=name, observed=entry.size, limit=file_limit)
                total += entry.size
                if total > total_limit:
                    raise ArchiveLimitError("archive content exceeds bound", kind="total_bytes",
                                            observed=total, limit=total_limit)
                stream = archive.extractfile(entry)
                if stream is None:
                    raise SandboxError("missing archive file data")
                value = stream.read(entry.size + 1)
                if len(value) != entry.size:
                    raise SandboxError("truncated archive file")
                if allowed is None or name in allowed:
                    found[name] = value
    except (tarfile.TarError, ValueError, EOFError) as exc:
        raise SandboxError("invalid archive") from exc
    if allowed is not None and set(found) != allowed:
        raise SandboxError("candidate removed a required source file")
    # A file cannot also be a directory ancestor, regardless of tar entry order.
    for name in found:
        if any(str(parent) in found for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise SandboxError("archive file/directory collision")
    return found


def write_files(destination: Path, files: dict[str, bytes]) -> None:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise SandboxError("destination is a symlink")
    for name, data in files.items():
        target = destination / relative_path(name)
        current = destination
        for part in PurePosixPath(name).parts[:-1]:
            current = current / part
            current.mkdir(exist_ok=True)
            if current.is_symlink() or not current.is_dir():
                raise SandboxError("destination contains a symlink")
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SandboxError("unsafe existing destination")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def source_receipt(files: dict[str, bytes]) -> dict:
    rows = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in sorted(files.items())}
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": 1, "sha256": digest, "files": rows}


def validate_gateway_ledger(ledger: bytes, *, started: bool, exit_code: int) -> str:
    records = []
    if ledger and not ledger.endswith(b"\n"):
        raise SandboxError("gateway ledger has a partial final line")
    for line in ledger.splitlines():
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise SandboxError("gateway ledger is not complete JSONL") from exc
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            raise SandboxError("invalid gateway ledger record")
        records.append(record)
    if started and (exit_code != 0 or not records or records[-1] !=
                    {"event": "stopped", "role": "broker", "clean": True}):
        raise SandboxError("gateway did not confirm complete evidence drain")
    return hashlib.sha256(ledger).hexdigest()


def solver_env(env: dict | None) -> dict | None:
    """Allow only explicit solver inputs, never ambient credential overlays."""
    allowed = {"CODEX_HOME", "HOME", "OPENAI_BASE_URL", "OPENAI_API_KEY", "PYTHONDONTWRITEBYTECODE"}
    result = {}
    for key, value in (env or {}).items():
        if key not in allowed or not isinstance(value, str):
            raise SandboxError("unapproved solver environment variable")
        if key == "OPENAI_API_KEY" and value != "openbench-sandbox-placeholder":
            raise SandboxError("provider credentials cannot enter the solver")
        if key == "OPENAI_BASE_URL" and value.rstrip("/") != f"http://127.0.0.1:{RELAY_PORT}":
            raise SandboxError("unapproved model route")
        if key in ("CODEX_HOME", "HOME"):
            path = PurePosixPath(value)
            if ".." in path.parts or not (path == PurePosixPath("/home/solver") or
                    PurePosixPath("/home/solver") in path.parents or PurePosixPath("/tmp") in path.parents):
                raise SandboxError("unapproved solver home")
        result[key] = value
    return result or None


def compose_config(image: str, token: str, *, cpus: float = 2, memory_mb: int = 2048) -> dict:
    validate_image(image)
    if not re.fullmatch(r"[a-f0-9]{24}", token):
        raise SandboxError("invalid trial namespace")
    if not 0 < cpus <= 8 or not 128 <= memory_mb <= 8192:
        raise SandboxError("resources exceed sandbox bounds")
    names = {key: f"obench-sandbox-{token}-{key}" for key in ("source", "logs", "gateway")}
    restrictions = {"image": image, "read_only": True, "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"], "pids_limit": 256,
                    "cpus": cpus, "mem_limit": f"{memory_mb}m", "restart": "no"}
    waiting_broker = (
        "import os,time; "
        "\nwhile not os.path.isfile('/run/private/start'): time.sleep(.05)\n"
        "os.execvp('python3',['python3','-m','obench.sandbox_gateway','broker',"
        "'--socket','" + RELAY_SOCKET + "','--config','/run/private/config.json',"
        "'--auth','/run/private/auth.json'])"
    )
    return {
        "services": {
            "main": {**restrictions, "container_name": f"obench-sandbox-{token}-solver",
                     "user": f"{UID}:{UID}", "network_mode": "none", "working_dir": "/app",
                     "entrypoint": ["/bin/sh", "-c"], "command": ["exec sleep infinity"],
                     "environment": {"HOME": "/home/solver", "PYTHONDONTWRITEBYTECODE": "1"},
                     "tmpfs": [f"/tmp:rw,nosuid,nodev,size=256m,uid={UID},gid={UID},mode=1777",
                               f"/home/solver:rw,nosuid,nodev,size=64m,uid={UID},gid={UID},mode=700"],
                     "volumes": [{"type": "volume", "source": "source", "target": "/app"},
                                 {"type": "volume", "source": "logs", "target": "/logs"},
                                 {"type": "volume", "source": "gateway", "target": "/run/openbench-model", "read_only": True}]},
            "broker": {**restrictions, "container_name": f"obench-sandbox-{token}-broker",
                       "user": f"0:{UID}", "entrypoint": ["python3", "-c"], "command": [waiting_broker],
                       "tmpfs": [f"/run/private:rw,noexec,nosuid,nodev,size=8m,uid=0,gid={UID},mode=700",
                                 "/tmp:rw,noexec,nosuid,nodev,size=16m"],
                       "volumes": [{"type": "volume", "source": "gateway", "target": "/run/openbench-model"}]},
        },
        "volumes": {key: {"name": name} for key, name in names.items()},
        "networks": {"default": {"name": f"obench-sandbox-{token}-network"}},
    }


def verify_inspection(info: dict, *, role: str, image_id: str, volume_names: dict[str, str]) -> None:
    host, config = info["HostConfig"], info["Config"]
    if info.get("Image") != image_id or config.get("User") != (f"{UID}:{UID}" if role == "main" else f"0:{UID}"):
        raise SandboxError("runtime image/user differs from sealed launch")
    if role == "main" and host.get("NetworkMode") != "none":
        raise SandboxError("solver has external networking")
    if not host.get("ReadonlyRootfs") or host.get("Privileged") or host.get("CapAdd"):
        raise SandboxError("container has unapproved privileges")
    if {value.upper() for value in host.get("CapDrop") or []} != {"ALL"}:
        raise SandboxError("container capabilities were not all dropped")
    if set(host.get("SecurityOpt") or []) not in ({"no-new-privileges"}, {"no-new-privileges:true"}):
        raise SandboxError("no-new-privileges is absent")
    if host.get("PidMode") or host.get("IpcMode") not in (None, "private") or host.get("UTSMode"):
        raise SandboxError("unapproved shared namespace")
    if host.get("UsernsMode") == "host" or host.get("CgroupnsMode") == "host" or (role == "broker" and host.get("NetworkMode", "").startswith(("host", "container:", "service:"))):
        raise SandboxError("unapproved host namespace")
    if host.get("Devices") or host.get("DeviceRequests") or host.get("ExtraHosts") or host.get("PortBindings"):
        raise SandboxError("unapproved device/host/port exposure")
    if not 0 < host.get("Memory", 0) <= 8192 * 1024 * 1024 or not 0 < host.get("NanoCpus", 0) <= 8_000_000_000:
        raise SandboxError("missing or excessive CPU/memory bounds")
    if not 0 < (host.get("PidsLimit") or 0) <= 256:
        raise SandboxError("missing process bound")
    expected = {"/run/openbench-model": (volume_names["gateway"], role != "main")}
    if role == "main":
        expected.update({"/app": (volume_names["source"], True), "/logs": (volume_names["logs"], True)})
    mounts = info.get("Mounts", [])
    if len(mounts) != len(expected):
        raise SandboxError("unexpected container mount count")
    for mount in mounts:
        if mount.get("Type") != "volume" or (mount.get("Name"), mount.get("RW")) != expected.get(mount.get("Destination")):
            raise SandboxError("unexpected host/shared mount")


async def docker_bytes(*args: str, data: bytes | None = None, limit: int = MAX_TRANSFER_BYTES,
                       timeout: float = 60) -> bytes:
    """Bound Docker output before parsing; never include stdin in exceptions."""
    process = await asyncio.create_subprocess_exec("docker", *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async def collect(stream, bound):
        chunks, size = [], 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > bound:
                raise TransferLimitError("Docker output exceeded its bound", observed=size, limit=bound)
            chunks.append(chunk)
    async def send():
        if data is not None:
            process.stdin.write(data)
            await process.stdin.drain()
            process.stdin.close()
    try:
        out, err, _ = await asyncio.wait_for(asyncio.gather(
            collect(process.stdout, limit), collect(process.stderr, 65536), send()), timeout)
        await asyncio.wait_for(process.wait(), timeout)
        if process.returncode:
            error = SandboxError("Docker operation failed (output withheld)")
            error.diagnostic_stderr = err
            raise error
        return out
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


def _build_environment_class(DockerEnvironment, EnvironmentCapabilities, NetworkMode):
    class RepairSandbox(DockerEnvironment):
        relay_socket = RELAY_SOCKET
        relay_port = RELAY_PORT

        def __init__(self, *args, runtime_image: str, max_requests: int = 200,
                     source_paths: list[str] | None = None,
                     request_timeout_seconds: float = 1200, **kwargs):
            self.runtime_image = validate_image(runtime_image)
            if type(max_requests) is not int or not 1 <= max_requests <= 10000:
                raise SandboxError("invalid gateway request budget")
            self.max_requests = max_requests
            if (isinstance(request_timeout_seconds, bool)
                    or not isinstance(request_timeout_seconds, (int, float))
                    or not math.isfinite(request_timeout_seconds)
                    or not 0 < request_timeout_seconds <= 3600):
                raise SandboxError("invalid gateway request timeout")
            self.request_timeout_seconds = request_timeout_seconds
            self._token = uuid.uuid4().hex[:24]
            self._private = tempfile.TemporaryDirectory(prefix="obench-sandbox-")
            self._sandbox_compose = Path(self._private.name) / "compose.json"
            self._started = self._sealed = self._gateway_started = self._deleted = False
            self._freeze_lock = asyncio.Lock()
            self._frozen_files = None
            self._receipt = None
            task = kwargs.get("task_env_config")
            if task is None or str(getattr(task.os, "value", task.os)) != "linux":
                raise SandboxError("repair sandbox requires Linux")
            if kwargs.get("extra_docker_compose_paths") or kwargs.get("extra_docker_compose") or kwargs.get("env") or kwargs.get("persistent_env") or getattr(task, "env", None):
                raise SandboxError("repair sandbox forbids extra compose/environment configuration")
            environment_dir = Path(kwargs.get("environment_dir", args[0] if args else "."))
            if (environment_dir / "docker-compose.yaml").exists():
                raise SandboxError("task-authored compose is forbidden")
            # Harbor provides ordinary log bind mounts. Strip only its exact
            # known bindings; reject any additional authority instead of ignoring it.
            trial_paths = kwargs.get("trial_paths")
            for mount in kwargs.pop("mounts", []) or []:
                target = mount.get("target")
                expected = {"/logs/agent": getattr(trial_paths, "agent_dir", None),
                            "/logs/verifier": getattr(trial_paths, "verifier_dir", None)}
                if target == "/logs/artifacts":
                    source = Path(mount.get("source", "")).resolve()
                    parent = Path(trial_paths.artifacts_dir).resolve()
                    if mount.get("type") != "bind" or parent not in source.parents:
                        raise SandboxError("unapproved artifact mount")
                elif target not in expected or expected[target] is None or mount.get("type") != "bind" or Path(mount.get("source", "")).resolve() != Path(expected[target]).resolve():
                    raise SandboxError("unapproved host mount")
            kwargs["mounts"] = []
            super().__init__(*args, **kwargs)
            self._seed_files = read_tree(self.environment_dir / "app")
            self._source_paths = ({relative_path(p) for p in source_paths} if source_paths else
                                  {p for p in self._seed_files if p.startswith("scripts/profiles/") and p.endswith(".py")})
            if not self._source_paths or not self._source_paths <= self._seed_files.keys():
                raise SandboxError("source allowlist must select existing workspace files")
            cpus = self._effective_cpus or 2
            memory = self._effective_memory_mb or 2048
            self._sandbox_config = compose_config(self.runtime_image, self._token, cpus=cpus, memory_mb=memory)
            self._volumes = {key: value["name"] for key, value in self._sandbox_config["volumes"].items()}
            self._containers = {key: value["container_name"] for key, value in self._sandbox_config["services"].items()}
            self._network = self._sandbox_config["networks"]["default"]["name"]
            self._sandbox_compose.write_text(json.dumps(self._sandbox_config))

        @staticmethod
        def _requires_egress_control(**kwargs):
            return False

        @property
        def capabilities(self):
            return EnvironmentCapabilities(disable_internet=True, mounted=False)

        def validate_network_policy_support(self, network_policy=None):
            policy = network_policy or self.network_policy
            if policy.network_mode != NetworkMode.NO_NETWORK or policy.allowed_hosts:
                raise SandboxError("repair sandbox only supports fixed no-network policy")

        def _validate_definition(self):
            return None  # The approved immutable runtime replaces task Dockerfiles.

        @property
        def _docker_compose_paths(self):
            return [self._sandbox_compose]

        async def _inspect(self, role):
            return json.loads(await docker_bytes("inspect", self._containers[role]))[0]

        async def start(self, force_build=False):
            if self._started or self._deleted:
                raise SandboxError("sandbox cannot be started twice")
            image = json.loads(await docker_bytes("image", "inspect", self.runtime_image))[0]
            if image.get("Os") != "linux":
                raise SandboxError("runtime image must be Linux")
            self._image_id = image["Id"]
            seed_code = """
import os, sys, tarfile
from pathlib import Path
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    for member in archive:
        path = Path('/app') / member.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.extractfile(member).read())
for name in ('agent', 'verifier', 'artifacts'):
    (Path('/logs') / name).mkdir(parents=True, exist_ok=True)
for root in (Path('/app'), Path('/logs')):
    for path in [*root.rglob('*'), root]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
        os.chown(path, 10001, 10001)
os.chmod('/run/openbench-model', 0o750)
os.chown('/run/openbench-model', 0, 10001)
"""
            mounts = []
            for key, target in (("source", "/app"), ("logs", "/logs"), ("gateway", "/run/openbench-model")):
                mounts += ["--mount", f"type=volume,source={self._volumes[key]},target={target}"]
            try:
                for name in self._volumes.values():
                    await docker_bytes("volume", "create", name)
                await docker_bytes("run", "--rm", "--name", "obench-sandbox-" + self._token + "-seed", "-i", "--network", "none", "--cap-drop", "ALL",
                    "--cap-add", "CHOWN", "--security-opt", "no-new-privileges", "--user", "0:10001",
                    *mounts, "--entrypoint", "python3", self.runtime_image, "-c", seed_code,
                    data=pack_files(self._seed_files))
                await self._run_docker_compose_command(["up", "--detach", "--wait"], timeout_sec=60)
                for role in ("main", "broker"):
                    verify_inspection(await self._inspect(role), role=role, image_id=self._image_id, volume_names=self._volumes)
                self._started = True
            except BaseException:
                await self._cleanup()
                raise

        async def start_gateway(self, model, effort, auth_path, max_requests=None):
            try:
                return await self._start_gateway(model, effort, auth_path, max_requests)
            except BaseException:
                if self._started:
                    await self.seal()
                raise

        async def _start_gateway(self, model, effort, auth_path, max_requests=None):
            if not self._started or self._sealed or self._gateway_started:
                raise SandboxError("gateway can start once in an active trial")
            budget = self.max_requests if max_requests is None else max_requests
            if type(budget) is not int or not 1 <= budget <= self.max_requests:
                raise SandboxError("gateway request budget exceeds trial budget")
            expected_gateway = hashlib.sha256(Path(__file__).with_name("sandbox_gateway.py").read_bytes()).hexdigest()
            observed_gateway = (await docker_bytes("exec", self._containers["broker"], "python3", "-c",
                "import hashlib,importlib.util; p=importlib.util.find_spec('obench.sandbox_gateway').origin; "
                "print(hashlib.sha256(open(p,'rb').read()).hexdigest())")).decode().strip()
            if observed_gateway != expected_gateway:
                raise SandboxError("runtime gateway code differs from reviewed host module")
            self._gateway_code_sha = expected_gateway
            auth = read_regular(Path(auth_path), 1024 * 1024)
            config = json.dumps({"model": model, "effort": effort, "max_requests": budget,
                                 "max_body_bytes": 8 * 1024 * 1024,
                                 "timeout_seconds": self.request_timeout_seconds}).encode()
            for name, data in (("auth.json", auth), ("config.json", config), ("start", b"ready")):
                await docker_bytes("exec", "-i", self._containers["broker"], "python3", "-c",
                    "import os,sys; p='/run/private/'+sys.argv[1]; "
                    "f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                    "os.write(f,sys.stdin.buffer.read()); os.close(f)", name, data=data)
            for _ in range(100):
                ready = await docker_bytes("exec", self._containers["broker"], "python3", "-c",
                    "import json,os,stat; p='" + RELAY_SOCKET + "'; "
                    "print(json.dumps(os.path.exists(p) and stat.S_ISSOCK(os.stat(p).st_mode)))")
                if json.loads(ready):
                    await docker_bytes("exec", "--detach", "--user", "10001:10001", self._containers["main"],
                        "sh", "-c", "exec python3 -m obench.sandbox_gateway relay --socket " + RELAY_SOCKET +
                        " --port " + str(RELAY_PORT) +
                        " --timeout-seconds " + str(self.request_timeout_seconds) +
                        " > /logs/agent/relay.log 2>&1")
                    for _ in range(100):
                        relay = await docker_bytes("exec", "--user", "10001:10001", self._containers["main"],
                            "python3", "-c", "import socket; s=socket.socket(); s.settimeout(.1); "
                            "print(s.connect_ex(('127.0.0.1'," + str(RELAY_PORT) + "))); s.close()")
                        if relay.strip() == b"0":
                            break
                        await asyncio.sleep(.1)
                    else:
                        raise SandboxError("relay did not become ready")
                    self._gateway_started = True
                    return
                await asyncio.sleep(.1)
            raise SandboxError("gateway did not become ready")

        async def seal(self):
            try:
                return await asyncio.wait_for(self._seal_containers(), SEAL_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise SandboxError("sandbox sealing exceeded its cleanup deadline") from exc

        async def _seal_containers(self):
            if self._sealed:
                return self._boundary_receipt()
            if not self._started:
                raise SandboxError("sandbox has not started")
            broker_info = None
            for role in ("broker", "main"):
                await docker_bytes("stop", "--time", "5", self._containers[role], timeout=20)
                info = await self._inspect(role)
                if info["State"].get("Running") or info["State"].get("Pid", 0) != 0:
                    raise SandboxError("container termination was not confirmed")
                if role == "broker":
                    broker_info = info
            ledger = await docker_bytes("logs", self._containers["broker"], limit=16 * 1024 * 1024)
            write_files(self.trial_paths.verifier_dir, {"sandbox-gateway.jsonl": ledger})
            self._gateway_ledger_sha = validate_gateway_ledger(ledger, started=self._gateway_started,
                                                              exit_code=broker_info["State"].get("ExitCode"))
            self._sealed = True
            return self._boundary_receipt()

        def _boundary_receipt(self):
            if not self._sealed:
                raise SandboxError("solver boundary is not sealed")
            return {"image_id": self._image_id, "solver_stopped": True, "broker_revoked": True,
                    "gateway_module_sha256": getattr(self, "_gateway_code_sha", None),
                    "gateway_ledger_sha256": self._gateway_ledger_sha}

        async def freeze_source(self, destination):
            async with self._freeze_lock:
                await self.seal()
                if self._frozen_files is None:
                    try:
                        archive = await docker_bytes("cp", self._containers["main"] + ":/app/.", "-")
                    except TransferLimitError as exc:
                        raise SandboxArtifactError("candidate source archive exceeds its bound", self._boundary_receipt()) from exc
                    try:
                        self._frozen_files = unpack_files(archive, allowed=self._source_paths)
                    except SandboxError as exc:
                        raise SandboxArtifactError(str(exc), self._boundary_receipt()) from exc
                    self._receipt = {**source_receipt(self._frozen_files), **self._boundary_receipt()}
                write_files(Path(destination), self._frozen_files)
                return dict(self._receipt)

        async def _download_logs(self, source, destination, *, single_file=False):
            archive = None
            try:
                # Harbor downloads logs after the timed agent phase, including
                # timeout/error recovery. Stop all solver processes before the
                # first export; freeze_source separately enforces the same gate.
                await self.seal()
                archive = await docker_bytes("cp", self._containers["main"] + ":" + source, "-")
                files = unpack_files(archive, file_limit=MAX_LOG_FILE_BYTES, total_limit=MAX_LOG_BYTES)
                if single_file:
                    name = PurePosixPath(source).name
                    if set(files) != {name}:
                        raise SandboxError("unexpected file export")
                    files = {Path(destination).name: files[name]}
                    destination = Path(destination).parent
                write_files(Path(destination), files)
            except (SandboxError, OSError, TimeoutError) as exc:
                # Harbor catches download errors without their reason. Preserve
                # bounded, content-free diagnostics outside the solver's logs.
                report = {"schema_version": 1, "source": source.removesuffix("/.")[:1024],
                          "archive_bytes": len(archive) if archive is not None else None,
                          "error_type": type(exc).__name__,
                          "error": str(exc) if isinstance(exc, SandboxError) else "log export I/O failed",
                          "violation": getattr(exc, "violation", None),
                          "limits": {"file_bytes": MAX_LOG_FILE_BYTES, "total_bytes": MAX_LOG_BYTES,
                                     "transfer_bytes": MAX_TRANSFER_BYTES, "files": MAX_FILES}}
                try:
                    write_files(self.trial_paths.verifier_dir / "sandbox-exports",
                                {uuid.uuid4().hex + ".json": (json.dumps(report) + "\n").encode()})
                except (SandboxError, OSError) as diagnostic_error:
                    self._log_export_error = "log export diagnostic persistence failed"
                    raise SandboxError(self._log_export_error) from diagnostic_error
                self._log_export_error = "log export failed; see verifier/sandbox-exports"
                raise

        async def download_dir(self, source_dir, target_dir):
            if source_dir.rstrip("/") == "/app":
                await self.freeze_source(target_dir)
                return
            if source_dir.rstrip("/") not in ("/logs/agent", "/logs/artifacts"):
                raise SandboxError("unapproved directory export")
            await self._download_logs(source_dir.rstrip("/") + "/.", target_dir)

        async def download_file(self, source_path, target_path):
            path = PurePosixPath(source_path)
            if not str(path).startswith("/logs/agent/") or ".." in path.parts:
                raise SandboxError("unapproved file export")
            await self._download_logs(str(path), target_path, single_file=True)

        async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
            if self._sealed or str(user) not in ("None", "10001", "10001:10001"):
                raise SandboxError("execution outside active solver identity")
            return await super().exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user="10001:10001")

        def _merge_env(self, env):
            # Harbor's agent.extra_env includes trusted HOST credential paths.
            # Deliberately do not merge persistent/scoped overlays into exec.
            return solver_env(env)

        async def upload_file(self, source_path, target_path):
            target = PurePosixPath(target_path)
            if self._sealed or not str(target).startswith(("/tmp/", "/home/solver/", "/logs/agent/")) or ".." in target.parts:
                raise SandboxError("unapproved solver upload")
            # This is host-authorized configuration, never auth.json. Credential
            # staging uses start_gateway's separate broker channel exclusively.
            if target.name == "auth.json":
                raise SandboxError("auth files cannot enter the solver")
            data = read_regular(Path(source_path))
            code = (
                "import os,sys; from pathlib import PurePosixPath; "
                "parts=PurePosixPath(sys.argv[1]).parts[1:]; "
                "fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY); "
                "\nfor part in parts[:-1]:\n"
                " try: os.mkdir(part,0o700,dir_fd=fd)\n"
                " except FileExistsError: pass\n"
                " new=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd); os.close(fd); fd=new\n"
                "out=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=fd); "
                "\nwith os.fdopen(out,'wb') as f: f.write(sys.stdin.buffer.read())\n"
                "os.close(fd)"
            )
            await docker_bytes("exec", "-i", "--user", "10001:10001", self._containers["main"],
                               "python3", "-c", code, str(target), data=data)

        async def upload_dir(self, source_dir, target_dir):
            if self._sealed and target_dir.rstrip("/") == "/logs/agent":
                # Harbor re-uploads host-derived trajectory logs after download.
                # The authoritative copy is already on the host; do not revive
                # the sealed solver for this redundant compatibility operation.
                return
            for name in read_tree(Path(source_dir)):
                await self.upload_file(Path(source_dir) / name, target_dir.rstrip("/") + "/" + name)

        async def prepare_logs_for_host(self):
            return None

        async def _cleanup(self):
            errors = []
            for name in [*self._containers.values(), "obench-sandbox-" + self._token + "-seed"]:
                try:
                    await docker_bytes("rm", "--force", name)
                except SandboxError:
                    # Missing containers after a failed start are harmless;
                    # existing survivors must be detected independently.
                    result = await docker_bytes("ps", "-aq", "--filter", "name=^/" + name + "$")
                    if result.strip():
                        errors.append(name)
            for name in self._volumes.values():
                try:
                    await docker_bytes("volume", "rm", name)
                except SandboxError:
                    existing = await docker_bytes("volume", "ls", "-q", "--filter", "name=^" + name + "$")
                    if existing.strip():
                        errors.append(name)
            try:
                await docker_bytes("network", "rm", self._network)
            except SandboxError:
                existing = await docker_bytes("network", "ls", "-q", "--filter", "name=^" + self._network + "$")
                if existing.strip():
                    errors.append(self._network)
            if errors:
                raise SandboxError("sandbox cleanup failed")
            self._deleted = True

        async def stop(self, delete=True):
            if self._deleted:
                return
            try:
                if self._started:
                    # Verifier/artifact collection owns source validation. A
                    # candidate rejection must not become a cleanup failure.
                    await self.seal()
            finally:
                await self._cleanup()
            if getattr(self, "_log_export_error", None):
                # Make failure visible even when Harbor swallowed the download
                # exception. Always remove containers/volumes before reporting it.
                raise SandboxError(self._log_export_error)

    RepairSandbox.__name__ = "RepairSandbox"
    RepairSandbox.__qualname__ = "RepairSandbox"
    RepairSandbox.__module__ = __name__
    return RepairSandbox


def __getattr__(name):
    if name != "RepairSandbox":
        raise AttributeError(name)
    from harbor.environments.capabilities import EnvironmentCapabilities
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import NetworkMode
    result = _build_environment_class(DockerEnvironment, EnvironmentCapabilities, NetworkMode)
    globals()[name] = result
    return result
