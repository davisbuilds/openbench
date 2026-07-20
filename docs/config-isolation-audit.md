# OpenBench harness config-isolation audit

Date: 2026-07-13. Scope: `bench/run.py` adapters, local and disposable-container execution. “Reads” below means host-owner files made reachable to the CLI (auth is listed separately and intentionally preserved).

| adapter | exec | config files read before fix | leakage | what leaked / final state |
|---|---|---|---|---|
| codex | local | `~/.codex/config.toml` plus owner CODEX_HOME resources (instructions, skills, MCP/plugins, rules/memories); `auth.json` | yes | Owner defaults/tools/instructions. **Fixed:** fresh CODEX_HOME, auth.json only; approved variance guards still disable apps/plugins/multi_agent. |
| codex | docker | mounted `~/.codex/config.toml` and `auth.json` into `/root/.codex` | yes | Same config.toml customization. **Fixed:** auth.json is the only mount; adapter composes fresh CODEX_HOME. |
| claude | local | none; existing adapter used fresh HOME and CLAUDE_CONFIG_DIR; auth from selected API-key env | no | No `~/.claude/CLAUDE.md`, settings.json, credentials, hooks/plugins. `--bare` is retained on every API-key lane as a billing boundary (no OAuth/keychain or Anthropic-billed side calls); `--disallowedTools Agent Task` pins the standard benchmark lane to one agent, in parity with Codex's `--disable multi_agent`; `DISABLE_AUTOUPDATER=1` preserves pinned-version provenance. Other DISABLE_* policy overrides remain removed. |
| claude | docker | none; no auth/config mount, selected API-key env only | no | Fresh image/container + adapter temp HOME; the same Agent/Task disallow pin applies. |
| pi | local | fresh HOME and copied `~/.pi/agent/auth.json`; inherited `PI_CODING_AGENT_DIR` could bypass HOME | yes (env override edge) | Owner settings/resources if override was set. **Fixed:** PI_CODING_AGENT_DIR forced into temp HOME; only auth copied; no `--no-extensions`, so factory resources remain. |
| pi | docker | host `.pi` tree was staged, though CLI's second temp HOME copied only auth | no CLI leak (overbroad exposure) | **Hardened:** mount only `.pi/agent/auth.json`; temp PI_CODING_AGENT_DIR. |
| opencode | local | XDG/global config (`~/.config/opencode`), OPENCODE_CONFIG/CONFIG_DIR/CONFIG_CONTENT, auth under `~/.local/share/opencode` or `~/.opencode/data` | yes | Providers, plugins, MCPs, permissions/instructions and other owner settings. **Fixed:** fresh HOME/XDG dirs, config env removed, auth.json only copied. |
| opencode | docker | entire auth/data and config directories mounted/staged | yes | `.config/opencode` and adjacent data. **Fixed:** auth.json files only; adapter re-isolates HOME/XDG. |
| cursor | local | `~/.cursor/cli-config.json` plus owner Cursor hooks/rules/skills/extensions; macOS desktop auth is coupled to owner state | yes | Model, permissions, network, hooks/rules/skills. **Fixed fail-closed:** fresh HOME/XDG; only Linux auth.json or authInfo projection copied, or CURSOR_API_KEY env. Current macOS desktop OAuth cannot authenticate after isolation (see blocker). |
| cursor | docker | dedicated container-auth `.config/cursor` and `.cursor` trees, plus legacy host `.cursor` fallback | yes | Mixed cli-config/customizations could enter container. **Fixed:** only dedicated `.config/cursor/auth.json` mounted. |
| grok | local | none; generated `~/.grok/config.toml` in fresh HOME, selected provider API-key env | no | No owner `~/.grok`. Generated `[model.<id>]` entries pin `base_url`, `api_backend`, `env_key`, and bearer auth. For gpt-5.6, the base URL is local CLIProxyAPI and the only child credential is optional CLIProxyAPI ingress auth (never `OPENAI_API_KEY`); CLIProxyAPI owns subscription OAuth. Benchmark parity deliberately disables subagents in every cell with both `GROK_SUBAGENTS=0` and `[subagents] enabled=false`; unrelated no-plan/web/memory policy overrides remain absent. |
| grok | docker | none; no host Grok mount, fresh container and adapter HOME | no | Open-model vendor keys are scoped per cell. The gpt-5.6 route receives only the non-secret CLIProxyAPI address and optional ingress key; subscription OAuth remains in the host daemon. The same generated routing/subagent guards apply. |

## Rotating refresh tokens and auth persist-back

Subscription CLIs can rotate single-use refresh tokens while running. After every CLI return (success, non-zero exit, or timeout), local Pi, OpenCode, Grok Build, and stock/ablation Codex adapters compare only the isolated `auth.json` with its master. Changed bytes are persisted through a mode-`0600`, fsynced temporary file and `os.replace`; identical files are untouched. Claude remains API-key-only.

Docker keeps auth staging read-only and mounts a private per-cell host directory read-write at `/bench/auth-return` only for `AUTH_PERSIST` allowlisted harnesses. `entry.py` returns only the declared auth file, then `docker_exec.py` applies the same comparison and atomic replacement. Grok returns to `~/.openbench/grok-container-auth/auth.json` when that dedicated credential supplied container `~/.grok/auth.json`, otherwise to the host Grok fallback. No config, sessions, or other isolated-HOME state is returned.

A mode-`0600` per-master lock serializes cooperating runners. Cells are sequential within one runner; independent concurrent runners remain last-completed-writer-wins because provider files expose no mergeable token generation. Operators should avoid benchmarking the same rotating login concurrently.

## Verification

- Full suite after follow-up corrections: `python3 -m unittest discover bench/tests` → **331 tests, OK** (18.313s).
- Authenticated runner smokes, local `tasks/make-it-run`, artifacts outside repo at `/tmp/openbench-config-smoke.YU0FtW`:
  - codex / gpt-5.5-medium: completed, checker score 1.0. Re-run after restoring feature guards also passed at `/tmp/openbench-codex-correction.Qj2JSC/codex.jsonl`.
  - pi / gpt-5.5-medium: completed, checker score 1.0.
  - opencode / gpt-5.5-medium: completed, checker score 1.0.
  - grokbuild / deepseek-v4-flash: completed, checker score 1.0.
  - cursor: fails authentication after isolation on this macOS host. The available desktop OAuth works only with the real HOME; dedicated Linux/container auth.json and projected authInfo are not accepted by the macOS binary, and CURSOR_API_KEY is unavailable. The adapter deliberately does not fall back to owner HOME.
  - claude: no ANTHROPIC_API_KEY is available on this host; this adapter was already non-leaking and API-key-only.
- Sentinel policy: the task explicitly forbids modifying personal config, so no sentinel was planted in real files. `bench/tests/test_codex_disable.py` uses subprocess capture to prove a fresh CODEX_HOME and absence of owner feature overrides; mount tests prove Docker receives auth-file paths only. Static temp-HOME construction proves owner config paths are unreachable for the other adapters. No secret values were recorded.

## Residual blocker/risk

Cursor local subscription OAuth on macOS is inseparable, with the installed CLI, from owner HOME state. Completing the required authenticated local Cursor smoke needs a `CURSOR_API_KEY` or a supported exportable auth file; using real HOME would reintroduce the exact leakage being fixed. Claude live smoke likewise needs ANTHROPIC_API_KEY, but Claude had no leaking lane before this change.
