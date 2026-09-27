# Profile tools development

Python, PyYAML and pytest are installed offline. Run:

```sh
python3 -m pytest -q
python3 -m compileall -q scripts/profiles
git status --short
git diff
```

The supplied public tests are standalone pre-fix rollout tests; they are a
starting point, not the hidden acceptance suite. Add scratch tests under tests/
when investigating. Source imports use `scripts` on Python's search path (see
tests/test_rollout.py). No live harness, model, or package install is needed.
Git has one synthetic baseline, with no original history or remotes. Only the
existing scripts/profiles/*.py files are submitted, as described in the task.
Use `rm -r` or Python's shutil for disposable directories; the harness may reject
`rm -f`. Python bytecode generation is disabled in the developer runtime.
