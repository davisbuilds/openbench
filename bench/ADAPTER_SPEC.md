# Adapter Spec (v1)

Each harness adapter is a Python module at `bench/adapters/<name>.py`.

## Required module-level API

```python
NAME: str                # canonical harness name, matches filename
MODELS: dict[str, str]   # canonical model name -> harness-specific model string
                         # canonical key required for M3: "gpt-5.5-medium"

def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict:
    """Run the harness headlessly on `instruction` with cwd=`workdir`.

    - `model` is the CANONICAL model name; the adapter maps it via MODELS.
    - Must never prompt interactively.
    - Must enforce timeout_s (Python subprocess timeout; macOS has no `timeout` cmd).
    - The agent's file edits must land in `workdir` (the runner passes a
      disposable copy of the task workspace).

    Returns:
    {
      "completed": bool,        # harness process exited 0 within timeout
                                # (NOT task success - the checker decides that)
      "error": str | None,      # timeout / crash / unsupported-model reason
      "output_tail": str,       # last ~2000 chars of combined stdout+stderr
      "tokens": int | None,     # if the harness reports usage, else None
      "turns": int | None,      # if the harness reports it, else None
      "cmd": list | str,        # what was executed (for the results log)
    }
    """
```

## Rules

- stdlib only (`subprocess`, `os`, `shutil`, `tempfile`, `json`, ...).
- Auth quirks live INSIDE the adapter:
  - `pi`: isolated `HOME` (temp dir) with only `~/.pi/agent/auth.json` copied in,
    so the user's personal extensions never load.
  - `opencode`: strip `OPENAI_API_KEY` from the child env to force subscription
    OAuth (stored credential at `~/.local/share/opencode/auth.json`).
  - `codex` / `cursor`: use the user's existing login as-is.
- Never modify the user's real config files (`~/.codex/config.toml`,
  `~/.pi/*`, `~/.cursor/*`, opencode config). Read-only use.
- Task success is decided by the runner's checker, never by the adapter.

## Runner contract (context)

The runner copies `tasks/<task>/workspace/` to a fresh temp dir, calls
`run()`, then executes `tasks/<task>/checker.sh` with cwd=that temp dir.
Checker exit 0 = task success. A built-in `null` adapter (does nothing,
returns completed=True) is used as a negative control.

## Verified model pins (canonical `gpt-5.5-medium`)

| Harness  | Invocation hint (verify against --help before relying on it)        |
|----------|---------------------------------------------------------------------|
| codex    | `codex exec -m gpt-5.5 -c model_reasoning_effort="medium" ...`       |
| pi       | `pi -p --model openai/gpt-5.5 ...` (thinking-level syntax `:medium`) |
| opencode | `opencode run -m openai/gpt-5.5 --variant medium ...`                |
| cursor   | `cursor-agent -p --force --model gpt-5.5-medium ...`                 |
| devin    | CLI at ~/.local/bin/devin - headless support unknown, investigate    |
