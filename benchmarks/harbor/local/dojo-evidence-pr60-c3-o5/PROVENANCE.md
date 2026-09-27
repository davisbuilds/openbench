# Dojo development case 3 / oracle 5

Source and grading behavior match historical `dojo-evidence-pr60-v5`.
The new case adds development instructions and two standalone public functions
from `tests/test_profiles_rollout.py` at pre-fix Dojo commit
`e0163e0fc489e6fee0b469a84fe80c2116e2ddda`:

- `test_a_session_with_no_model_call_is_absent_not_empty`
- `test_find_rollouts_on_a_missing_root_is_empty_not_an_error`

Their bodies are copied verbatim, with a minimal import header. No captured
sessions, skill catalog, hidden oracle or reference repair is included.
Source construction/licensing: [historical provenance](../dojo-evidence-pr60-v4/PROVENANCE.md).
The case revision advances for the additional public tests/instructions; oracle
5 is unchanged. New runs use a developer image and new runtime admission.
