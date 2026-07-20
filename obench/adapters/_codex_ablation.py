"""Shared runtime CODEX_HOME composer for Codex ablation adapters.

Docker mechanism: ``bench/docker_exec.py`` treats ``codex_v1``/``codex_v2`` as
Codex-like harnesses, staging the same read-only ``~/.codex/auth.json`` surface
as stock ``codex`` and mounting only the selected repository variant directory
(e.g. ``ablation/codex-home-v1``) at ``/bench/ablation/codex-home-v1:ro``. This
helper then composes a fresh writable CODEX_HOME inside the running
host/container temp space, copying only the variant config, instructions file,
and the staged runtime auth. No auth file is ever copied into the repo or baked
into the image.
"""

import importlib.util
import json
import os
import re
import shutil
import tempfile

from auth_persist import try_persist_auth_file

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CONTAINER_ABLATION = "/bench/ablation"
_INSTRUCTIONS_RE = re.compile(r'(?m)^(\s*model_instructions_file\s*=\s*)(["\'])([^"\']+)\2\s*$')
_PROVIDER_RE = re.compile(
    r'(?ms)^\s*model_provider\s*=\s*["\']deepseek_bridge["\']\s*\n'
    r'|^\s*\[model_providers\.deepseek_bridge\]\s*\n.*?(?=^\s*\[|\Z)'
)


def _load_sibling(module_name):
    path = os.path.join(_HERE, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(f"openbench_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_codex = _load_sibling("codex")
MODELS = _codex.MODELS
OPEN_MODELS = _codex.OPEN_MODELS


def version():
    return _codex.version()


def _ablation_root():
    if os.environ.get("BENCH_IN_CONTAINER") and os.path.isdir(_CONTAINER_ABLATION):
        return _CONTAINER_ABLATION
    return os.path.join(_REPO_ROOT, "ablation")


def _source_codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


def _rewrite_config(config_text, instructions_abs):
    """Return variant config text with runtime-safe absolute instruction path.

    The checked-in ablation configs include a DeepSeek capture-proxy provider for
    the original measurement spike. Benchmark routing remains owned by
    ``codex.py`` (CLI ``-c`` overrides for open models; default provider for
    first-party subscription models), so that probe-only provider block is
    dropped while preserving the actual ablation knobs.
    """
    config_text = _PROVIDER_RE.sub("", config_text)
    if not _INSTRUCTIONS_RE.search(config_text):
        raise ValueError("variant config missing model_instructions_file")
    return _INSTRUCTIONS_RE.sub(
        lambda m: f"{m.group(1)}{json.dumps(instructions_abs)}",
        config_text,
        count=1,
    )


def _variant_paths(variant, ablation_root=None):
    root = ablation_root or _ablation_root()
    variant_dir = os.path.join(root, f"codex-home-{variant}")
    config_path = os.path.join(variant_dir, "config.toml")
    with open(config_path, encoding="utf-8") as fh:
        config_text = fh.read()
    match = _INSTRUCTIONS_RE.search(config_text)
    if not match:
        raise ValueError(f"{config_path} missing model_instructions_file")
    instructions_ref = match.group(3)
    instructions_src = instructions_ref if os.path.isabs(instructions_ref) else os.path.join(variant_dir, instructions_ref)
    return config_path, instructions_src, config_text


def compose_codex_home(variant, codex_home, *, source_codex_home=None, ablation_root=None):
    """Populate ``codex_home`` for an ablation run and return path metadata.

    ``auth.json`` is copied from ``source_codex_home`` (or runtime ``$CODEX_HOME``
    / ``~/.codex``) into the temp home. The config's ``model_instructions_file``
    is rewritten to an absolute path inside the temp home so it resolves in both
    local and container execution.
    """
    os.makedirs(codex_home, exist_ok=True)
    _config_src, instructions_src, config_text = _variant_paths(variant, ablation_root)
    if not os.path.isfile(instructions_src):
        raise FileNotFoundError(f"variant instructions not found: {instructions_src}")

    instructions_dst = os.path.join(codex_home, os.path.basename(instructions_src))
    shutil.copy2(instructions_src, instructions_dst)

    config_dst = os.path.join(codex_home, "config.toml")
    with open(config_dst, "w", encoding="utf-8") as fh:
        fh.write(_rewrite_config(config_text, os.path.abspath(instructions_dst)))

    auth_home = os.path.expanduser(source_codex_home or _source_codex_home())
    auth_src = os.path.join(auth_home, "auth.json")
    if not os.path.isfile(auth_src):
        raise FileNotFoundError(f"missing Codex auth.json at {auth_src}")
    auth_dst = os.path.join(codex_home, "auth.json")
    shutil.copy2(auth_src, auth_dst)

    return {
        "codex_home": os.path.abspath(codex_home),
        "config": config_dst,
        "instructions": instructions_dst,
        "auth": auth_dst,
        "auth_source": auth_src,
    }


def _setup_needed(exc):
    return {
        "completed": False,
        "error": f"SETUP-NEEDED: {exc}",
        "output_tail": "",
        "tokens": None,
        "turns": None,
        "cmd": None,
        **_codex._empty_token_usage(),
    }


def run_variant(name, variant, instruction, workdir, model, timeout_s):
    with tempfile.TemporaryDirectory(prefix=f"{name}_codex_home_") as codex_home:
        try:
            metadata = compose_codex_home(variant, codex_home)
        except (OSError, ValueError) as exc:
            return _setup_needed(exc)
        try:
            return _codex.run(
                instruction,
                workdir,
                model,
                timeout_s,
                env_override={"CODEX_HOME": codex_home},
            )
        finally:
            try_persist_auth_file(metadata["auth"], metadata["auth_source"])
