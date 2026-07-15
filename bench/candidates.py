"""Declarative candidate adapters for config variants and arbitrary CLIs."""

import hashlib
import importlib.util
import inspect
import json
import os
import signal
import shutil
import subprocess
import tempfile
import tomllib


def _load_adapter(adapters_dir, name):
    path = os.path.join(adapters_dir, f"{name}.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"adapter not found: {path}")
    spec = importlib.util.spec_from_file_location(f"bench_candidate_base_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _resolve(spec_path, value):
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(os.path.dirname(spec_path), value)
    return os.path.abspath(value)


def _expand(value, values):
    """Replace only documented placeholders; preserve unrelated JSON/TOML braces."""
    text = str(value)
    for name, replacement in values.items():
        text = text.replace("{" + name + "}", str(replacement))
    return text


def _safe_destination(root, relative):
    """Resolve a candidate-owned destination without escaping its temp root."""
    if os.path.isabs(relative):
        raise ValueError(f"destination must be relative: {relative!r}")
    root = os.path.abspath(root)
    destination = os.path.abspath(os.path.join(root, relative))
    if os.path.commonpath((root, destination)) != root:
        raise ValueError(f"destination escapes candidate directory: {relative!r}")
    return destination


def _run_process(cmd, *, cwd, timeout, env):
    """Run a manifest command and contain its complete process group."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, stdin=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        stdout, stderr = proc.communicate()
        exc.stdout, exc.stderr = stdout, stderr
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


_SAFE_ENV_NAMES = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TEMP", "TMP",
    "SystemRoot", "WINDIR", "PATHEXT",
}
_PROXY_ENV_NAMES = {
    "OPENBENCH_PROXY", "OPENBENCH_PROXY_BASE_URL", "OPENBENCH_PROXY_CELL_TOKEN",
}


def _manifest_environ(inherit_env, pass_env):
    if inherit_env:
        return dict(os.environ)
    names = _SAFE_ENV_NAMES | _PROXY_ENV_NAMES | set(pass_env)
    return {name: os.environ[name] for name in names if name in os.environ}


def _validate_auth_files(auth_files):
    for auth in auth_files:
        source = auth.get("source", "")
        if not source.startswith("~/") or ".." in source.split("/"):
            raise ValueError("auth file sources must be home-relative paths beginning with '~/'.")


def _base_result(completed, error, output, cmd):
    return {
        "completed": completed, "error": error, "output_tail": output[-2000:],
        "full_output": output, "tokens": None, "turns": None, "cmd": cmd,
        "tokens_input_uncached": None, "tokens_cache_read": None,
        "tokens_cache_write": None, "tokens_output": None,
        "tokens_reasoning": None, "usage_raw": None, "token_basis": None,
    }


class ConfigVariant:
    kind = "config-variant"

    def __init__(self, path, data, adapters_dir):
        self.path = os.path.abspath(path)
        self.name = data["name"]
        self.base_adapter = data["base_adapter"]
        self.config_dir = os.environ.get("OPENBENCH_CANDIDATE_CONFIG_DIR") or _resolve(
            self.path, data["config_dir"])
        self.config_dir = os.path.abspath(self.config_dir)
        self.config_files = data.get("config_files")
        self.env = {str(k): str(v) for k, v in data.get("env", {}).items()}
        self.auth_files = data.get("auth_files", [])
        _validate_auth_files(self.auth_files)
        self.module = _load_adapter(adapters_dir, self.base_adapter)
        self.provenance = self._provenance()
        self.proxy_adapter = self.base_adapter

    def _provenance(self):
        entries = self.config_files or [
            p for p in sorted(os.listdir(self.config_dir))
            if os.path.isfile(os.path.join(self.config_dir, p))
        ]
        sources = [entry if isinstance(entry, str) else entry["source"] for entry in entries]
        files = {name: _sha256(os.path.join(self.config_dir, name)) for name in sources}
        return {"kind": self.kind, "name": self.name, "base_adapter": self.base_adapter,
                "spec": self.path, "spec_sha256": _sha256(self.path),
                "config_dir": self.config_dir, "config_files_sha256": files,
                "config_files": entries, "env_names": sorted(self.env),
                "auth_files": [{"source": a["source"], "destination": a["destination"]}
                               for a in self.auth_files]}

    def version(self):
        fn = getattr(self.module, "version", None)
        return fn() if callable(fn) else None

    def run(self, instruction, workdir, model, timeout_s):
        with tempfile.TemporaryDirectory(prefix=f"{self.name}_config_") as staged:
            values = {"config_dir": staged, "workspace": workdir, "model": model}
            if self.config_files:
                for entry in self.config_files:
                    item = {"source": entry, "destination": entry} if isinstance(entry, str) else entry
                    src = os.path.join(self.config_dir, item["source"])
                    dst = _safe_destination(staged, item.get("destination", item["source"]))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if item.get("template"):
                        with open(src, encoding="utf-8") as fh:
                            text = _expand(fh.read(), values)
                        with open(dst, "w", encoding="utf-8") as fh:
                            fh.write(text)
                    else:
                        shutil.copy2(src, dst)
            else:
                shutil.copytree(self.config_dir, staged, dirs_exist_ok=True)
            for auth in self.auth_files:
                src = os.path.expanduser(auth["source"])
                dst = _safe_destination(staged, _expand(auth["destination"], values))
                if not os.path.isfile(src):
                    return _base_result(False, f"SETUP-NEEDED: missing auth file {src}", "", None)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            env = {key: _expand(value, values) for key, value in self.env.items()}
            if "env_override" in inspect.signature(self.module.run).parameters:
                return self.module.run(instruction, workdir, model, timeout_s,
                                       env_override=env)
            old = {key: os.environ.get(key) for key in env}
            try:
                os.environ.update(env)
                return self.module.run(instruction, workdir, model, timeout_s)
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class ManifestHarness:
    kind = "manifest"

    def __init__(self, path, data):
        self.path = os.path.abspath(path)
        self.name = data["name"]
        self.base_adapter = None
        self.command = data["command"]
        if not isinstance(self.command, list) or not all(isinstance(x, str) for x in self.command):
            raise ValueError("manifest command must be an array of strings")
        self.models = data.get("models", {})
        self.env = {str(k): str(v) for k, v in data.get("env", {}).items()}
        self.inherit_env = bool(data.get("inherit_env", False))
        self.pass_env = data.get("pass_env", [])
        if not isinstance(self.pass_env, list) or not all(isinstance(x, str) for x in self.pass_env):
            raise ValueError("manifest pass_env must be an array of names")
        self.unset_env = data.get("unset_env", [])
        self.isolate_home = bool(data.get("isolate_home", True))
        self.auth_files = data.get("auth_files", [])
        _validate_auth_files(self.auth_files)
        if self.auth_files and not self.isolate_home:
            raise ValueError("manifest auth_files require isolate_home=true")
        self.version_command = data.get("version_command")
        if (self.version_command is not None
                and (not isinstance(self.version_command, list)
                     or not all(isinstance(x, str) for x in self.version_command))):
            raise ValueError("manifest version_command must be an array of strings")
        self.base_url_env = data.get("base_url_env")
        self.proxy_route = data.get("proxy_route")
        if bool(self.base_url_env) != bool(self.proxy_route):
            raise ValueError("manifest proxy routing requires both base_url_env and proxy_route")
        self.proxy_adapter = data.get("proxy_adapter")
        self.provenance = {"kind": self.kind, "name": self.name, "spec": self.path,
                           "spec_sha256": _sha256(self.path), "command": list(self.command),
                           "models": dict(self.models), "env_names": sorted(self.env),
                           "inherit_env": self.inherit_env,
                           "pass_env": sorted(self.pass_env),
                           "auth_files": [{"source": a["source"], "destination": a["destination"]}
                                          for a in self.auth_files],
                           "version_command": list(self.version_command or []),
                           "base_url_env": self.base_url_env,
                           "proxy_route": self.proxy_route}

    def version(self):
        if not self.version_command:
            return None
        try:
            proc = subprocess.run(
                self.version_command, capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
                env=_manifest_environ(self.inherit_env, self.pass_env),
            )
        except Exception:
            return None
        out = (proc.stdout or proc.stderr or "").strip()
        return out or None

    def run(self, instruction, workdir, model, timeout_s):
        if self.models and model not in self.models:
            return _base_result(
                False, f"unsupported-model: {model!r} (have {list(self.models)})", "", None)
        model_id = self.models.get(model, model)
        home_ctx = tempfile.TemporaryDirectory(prefix=f"{self.name}_home_") if self.isolate_home else None
        home = home_ctx.name if home_ctx else os.path.expanduser("~")
        try:
            env = _manifest_environ(self.inherit_env, self.pass_env)
            if self.isolate_home:
                env["HOME"] = home
            values = {"prompt": instruction, "workspace": workdir, "model": model_id, "home": home}
            for key in self.unset_env:
                env.pop(key, None)
            env.update({key: _expand(value, values) for key, value in self.env.items()})
            for auth in self.auth_files:
                src = os.path.expanduser(auth["source"])
                dst = _safe_destination(home, _expand(auth["destination"], values))
                if not os.path.isfile(src):
                    return _base_result(False, f"SETUP-NEEDED: missing auth file {src}", "", None)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            if self.base_url_env and self.proxy_route and env.get("OPENBENCH_PROXY"):
                base, token = env.get("OPENBENCH_PROXY_BASE_URL"), env.get("OPENBENCH_PROXY_CELL_TOKEN")
                if base and token:
                    env[self.base_url_env] = "/".join(
                        [base.rstrip("/"), "cell", token, self.proxy_route.strip("/")])
            cmd = [_expand(part, values) for part in self.command]
            try:
                proc = _run_process(cmd, cwd=workdir, timeout=timeout_s, env=env)
            except subprocess.TimeoutExpired as exc:
                out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
                err = ((exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                return _base_result(False, f"timeout after {timeout_s}s", out + err, cmd)
            output = (proc.stdout or "") + (proc.stderr or "")
            return _base_result(proc.returncode == 0, None if proc.returncode == 0 else f"exit {proc.returncode}", output, cmd)
        finally:
            if home_ctx:
                home_ctx.cleanup()


def load_candidate(path, adapters_dir):
    path = os.path.abspath(path)
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    kind = data.get("kind", "manifest")
    if kind == "config-variant":
        return ConfigVariant(path, data, adapters_dir)
    if kind == "manifest":
        return ManifestHarness(path, data)
    raise ValueError(f"unknown candidate kind {kind!r} in {path}")


def load_candidates(paths, adapters_dir):
    result = {}
    for path in paths:
        candidate = load_candidate(path, adapters_dir)
        if candidate.name in result:
            raise ValueError(f"duplicate candidate name {candidate.name!r}")
        result[candidate.name] = candidate
    return result
