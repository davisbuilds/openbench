"""Standalone public pre-fix rollout tests; no captured sessions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from profiles import rollout_codex
from profiles.rollout_codex import read_rollout

def test_a_session_with_no_model_call_is_absent_not_empty(tmp_path):
    """A rollout with no skills block means nothing was sent.

    Distinguishable from a listing of zero entries, because "nobody looked" and
    "the harness sent nothing" are different facts — the same confusion that made
    an unmeasured absence read as an observed one in Task 3.
    """
    path = tmp_path / "rollout-empty.jsonl"
    path.write_text('{"type":"session_meta","payload":{"originator":"codex-tui"}}\n')
    assert read_rollout(path) is None

def test_find_rollouts_on_a_missing_root_is_empty_not_an_error():
    assert rollout_codex.find_rollouts(Path("/nonexistent/sessions")) == []
