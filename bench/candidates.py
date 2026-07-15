"""Declarative candidate adapters for config variants and arbitrary CLIs."""

import hashlib
import importlib.util
import json
import os
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
    return str(value).format_map(values)


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
        self.config_dir = _resolve(self.path, data["config_dir"])
        self.config_files = data.get("config_files")
        self.env = {str(k): str(v) for k, v in data.get("env", {}).items()}
        self.auth_files = data.get("auth_files", [])
        self.module = _load_adapter(adapters_dir, self.base_adapter)
        self.provenance = self._provenance()
        self.proxy_adapter = self.base_adapter

    def _provenance(self):
        names = self.config_files or [
            p for p in sorted(os.listdir(self.config_dir))
            if os.path.isfile(os.path.join(self.config_dir, p))
        ]
        files = {name: _sha256(os.path.join(self.config_dir, name)) for name in names}
        return {"kind": self.kind, "name": self.name, "base_adapter": self.base_adapter,
                "spec": self.path, "spec_sha256": _sha256(self.path),
                "config_dir": self.config_dir, "config_files_sha256": files,
                "env_names": sorted(self.env),
                "auth_files": [{"source": a["source"], "destination": a["destination"]}
                               for a in self.auth_files]}

    def version(self):
        fn = getattr(self.module, "version", None)
        return fn() if callable(fn) else None

    def run(self, instruction, workdir, model, timeout_s):
        with tempfile.TemporaryDirectory(prefix=f"{self.name}_config_") as staged:
            if self.config_files:
                for rel in self.config_files:
                    src, dst = os.path.join(self.config_dir, rel), os.path.join(staged, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
            else:
                shutil.copytree(self.config_dir, staged, dirs_exist_ok=True)
            values = {"config_dir": staged, "workspace": workdir, "model": model}
            for auth in self.auth_files:
                src = os.path.expanduser(auth["source"])
                dst = _expand(auth["destination"], values)
                dst = dst if os.path.isabs(dst) else os.path.join(staged, dst)
                if not os.path.isfile(src):
                    return _base_result(False, f"SETUP-NEEDED: missing auth file {src}", "", None)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            env = {key: _expand(value, values) for key, value in self.env.items()}
            try:
                return self.module.run(instruction, workdir, model, timeout_s,
                                       env_override=env)
            except TypeError as exc:
                if "env_override" not in str(exc):
                    raise
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
        self.unset_env = data.get("unset_env", [])
        self.isolate_home = bool(data.get("isolate_home", False))
        self.auth_files = data.get("auth_files", [])
        self.version_command = data.get("version_command")
        self.base_url_env = data.get("base_url_env")
        self.proxy_route = data.get("proxy_route")
        self.proxy_adapter = data.get("proxy_adapter")
        self.provenance = {"kind": self.kind, "name": self.name, "spec": self.path,
                           "spec_sha256": _sha256(self.path), "command": list(self.command),
                           "models": dict(self.models), "env_names": sorted(self.env),
                           "auth_files": [{"source": a["source"], "destination": a["destination"]}
                                          for a in self.auth_files],
                           "version_command": list(self.version_command or []),
                           "base_url_env": self.base_url_env,
                           "proxy_route": self.proxy_route}

    def version(self):
        if not self.version_command:
            return None
        try:
            proc = subprocess.run(self.version_command, capture_output=True, text=True,
                                  timeout=5, stdin=subprocess.DEVNULL)
        except Exception:
            return None
        out = (proc.stdout or proc.stderr or "").strip()
        return out or None

    def run(self, instruction, workdir, model, timeout_s):
        model_id = self.models.get(model, model)
        home_ctx = tempfile.TemporaryDirectory(prefix=f"{self.name}_home_") if self.isolate_home else None
        home = home_ctx.name if home_ctx else os.path.expanduser("~")
        try:
            env = dict(os.environ)
            if self.isolate_home:
                env["HOME"] = home
            values = {"prompt": instruction, "workspace": workdir, "model": model_id, "home": home}
            for key in self.unset_env:
                env.pop(key, None)
            env.update({key: _expand(value, values) for key, value in self.env.items()})
            for auth in self.auth_files:
                src = os.path.expanduser(auth["source"])
                dst = _expand(auth["destination"], values)
                dst = dst if os.path.isabs(dst) else os.path.join(home, dst)
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
                proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                                      timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
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
