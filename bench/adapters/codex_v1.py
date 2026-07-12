"""Codex V1 ablation harness: compact pi-style base prompt.

At run time this adapter composes a temporary CODEX_HOME from
``ablation/codex-home-v1`` with ``model_instructions_file`` rewritten to an
absolute path inside that temp home, then copies only runtime ``auth.json`` from
the real ``$CODEX_HOME``/``~/.codex``. Docker mode mounts the same read-only
Codex auth surface as stock ``codex`` plus only this variant directory at
``/bench/ablation/codex-home-v1:ro``; composition happens inside the container
before delegating to ``codex.py``. Auth is never stored in the repo or image.
"""

import importlib.util
import os

NAME = "codex_v1"
_VARIANT = "v1"


def _load_helper():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_codex_ablation.py")
    spec = importlib.util.spec_from_file_location("openbench_codex_ablation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_helper = _load_helper()
MODELS = _helper.MODELS
OPEN_MODELS = _helper.OPEN_MODELS
version = _helper.version
compose_codex_home = _helper.compose_codex_home


def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    return _helper.run_variant(NAME, _VARIANT, instruction, workdir, model, timeout_s)
