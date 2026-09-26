# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `feal-differential-cryptanalysis` (original-tasks/feal-differential-cryptanalysis)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE)
- **Upstream difficulty**: `hard`

## Modifications made during conversion

- `instruction.md`: upstream `instruction` prose, adapted so paths are relative
  to the working directory (`feal.py`, `attack.py`) instead of `/app/...`.
- `workspace/feal.py`: the reference FEAL-like cipher, copied verbatim from the
  upstream task.
- `checker.sh` + `checker_data/run_checks.py`: **the checker's verification basis
  was changed.** The upstream `tests/test_outputs.py` compiles a C
  implementation of FEAL (`tests/feal_module.c` via `setup.py build_ext`) and
  checks `feal_in_c.get_keys()[5] == attack.attack(feal_in_c.encrypt)`. The
  openbench-harness image has no C toolchain, so this checker verifies the
  attack against the equivalent reference **pure-Python** cipher instead. Like
  upstream's C build, the oracle is **checker-owned**: the cipher is loaded from
  `checker_data/feal_ref.py` in the read-only task dir (never from the
  agent-editable workspace copy of `feal.py`), so doctoring workspace files
  cannot pin or leak the key. The checker calls `feal_ref.create_random_keys()`,
  runs `attack.attack(feal_ref.encrypt)`, and asserts the result equals
  `feal_ref.key[5]`. Because the attack is randomized, the checker requires
  success on 5 independent random keys (the reference solution passed 20/20 in
  testing).
- Known residual vs upstream: upstream's oracle is a compiled C module, so the
  key is not introspectable from Python; our pure-Python `encrypt` necessarily
  exposes its module globals to a deliberately adversarial `attack.py` (e.g. via
  `encrypt_fn.__globals__`). Workspace doctoring — the realistic reward-hack —
  is closed; callable introspection is accepted and documented rather than
  papered over.
- `solution/attack.py`: the reference attack extracted verbatim from the upstream
  `solution.sh`.
