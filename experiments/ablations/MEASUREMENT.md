# Harness-Bloat Ablation Spike

## Scope

- Installed CLI tested: `codex-cli 0.144.0` at `/opt/homebrew/bin/codex` (the prompt expected `0.144.1`; this spike reports the installed binary actually present).
- Live route: DeepSeek `deepseek-v4-flash` only, through `bench/openmodel_bridge.sh` plus `ablation/tools/capture_proxy.py` on localhost.
- No benchmark matrix was run; probes used one trivial prompt per variant plus targeted config probes.
- Capture artifacts are under `ablation/captures/`; request headers are scrubbed and Codex was given only a placeholder ingress key.

## Method

`ablation/tools/run_probes.sh` starts or reuses the OpenBench LiteLLM bridge, runs a local capture proxy, and invokes:

- Codex V0/V1/V2 with separate `CODEX_HOME` directories in `ablation/codex-home-v*/`.
- Pi through its normal CLI path with a temporary provider extension, `--no-extensions`, and `--no-context-files` so the measurement is the fixed pi harness prompt plus tool schemas.

Token counts come from `ablation/tools/measure_payloads.py`. `tiktoken` was not installed in this environment, so the recorded method is `regex_approx_words_punct`: a deterministic GPT-style regex approximation over words/numbers/punctuation. Word counts use whitespace-like word matching. Component definitions:

- **Base instructions**: Codex `instructions` field or pi first system message.
- **Tool schemas**: serialized `tools` array sent on the request.
- **Extra blocks**: Codex permissions, environment, apps/collab/skills blocks found in request input items.
- **Project docs**: `AGENTS.md` / project instruction payloads found in request input items.

## Knob Verdicts

| Knob | Verdict | Evidence |
| --- | --- | --- |
| `model_instructions_file` | WORKS | V0 request `instructions_chars=20751`; V1/V2 `instructions_chars=873` and begin with the custom pi-style prompt in `ablation/captures/v1/001-20260712T142922679433Z.json` and `ablation/captures/v2/001-20260712T142924436151Z.json`. |
| `include_permissions_instructions=false` | WORKS | V1 has `<permissions instructions>` in input item 0; V2 has one input item only and marker `has_permissions=false` in `ablation/measurement.json`. |
| `include_environment_context=false` | WORKS | V1 includes `<environment_context>`; V2 marker `has_environment=false` and input items drop from 3 to 1 in `ablation/measurement.json`. |
| `include_apps_instructions=false` | WORKS | No app block appears in V2; knob accepted by installed binary and V2 drops all optional developer/context blocks. |
| `include_collaboration_mode_instructions=false` | WORKS | No collaboration block appears in V2; knob accepted by installed binary and V2 drops all optional developer/context blocks. |
| `[skills] include_instructions=false` | WORKS | V1 includes `<skills_instructions>`; V2 marker `has_skills=false` in `ablation/measurement.json`. |
| `project_doc_max_bytes=4096` | WORKS | Project-doc probe: V0 project docs `32910 chars / 5782 tokens`; V2 project docs `4238 chars / 751 tokens`, both truncated before `PROJECT_DOC_END`, in `ablation/captures/project-docs-v*.json`. |
| Unknown key, default config | IGNORED | `ablation/captures/unknown-key/config.toml` with `unknown_ablation_probe_key=true` still produced a request in `ablation/captures/unknown-key/001-20260712T143140903366Z.json`. |
| Unknown key with `--strict-config` | ERROR | `ablation/captures/unknown-key/strict.stderr.txt` reports `unknown configuration field unknown_ablation_probe_key`. |

## Context Size: Main Probe

| Variant | Base words | Base tokens | Tool words | Tool tokens | Extra words | Extra tokens | Project-doc words | Project-doc tokens | Total words | Total tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| V0 | 3259 | 4458 | 1453 | 4311 | 1504 | 2966 | 0 | 0 | 6216 | 11735 |
| V1 | 128 | 172 | 1453 | 4311 | 1504 | 2966 | 0 | 0 | 3085 | 7449 |
| V2 | 128 | 172 | 1453 | 4311 | 0 | 0 | 0 | 0 | 1581 | 4483 |
| pi | 321 | 585 | 278 | 904 | 0 | 0 | 0 | 0 | 599 | 1489 |


## Project-Docs Probe

| Variant | Project-doc chars | Project-doc words | Project-doc tokens | Total fixed tokens with docs |
| --- | ---: | ---: | ---: | ---: |
| V0 project docs | 32910 | 5753 | 5782 | 17537 |
| V2 project docs | 4238 | 722 | 751 | 5234 |

## V1 Prompt Text

```text
You are an expert coding assistant operating inside Codex CLI. Help users by inspecting files, running shell commands, editing code, and giving concise status updates.

Work carefully and verify changes before reporting success. Prefer small, focused edits. Use file paths clearly. Do not expose secrets.

Shell/tool mechanics:
- Use shell commands to inspect files, run tests, and gather evidence. Prefer fast search tools such as `rg` and `rg --files` when available.
- Use `apply_patch` for file edits. Invoke it as a shell command named `apply_patch` with a patch body; do not use similarly named commands.
- Request elevated permissions only when the configured sandbox/approval mode requires it and the action is necessary.
- Respect repository instructions such as `AGENTS.md` that apply to touched files.
- Keep final answers concise and include validation results.

```

## 10x-Premise Verdict

Supported for installed Codex `0.144.0` on this probe, depending on whether tool schemas are counted:

- Base instructions only: Codex V0 `4458` approx tokens vs pi `585` approx tokens = `7.6x`.
- Base plus optional extra blocks: Codex V0 `7424` approx tokens vs pi `585` approx tokens = `12.7x`.
- Full fixed request including tool schemas: Codex V0 `11735` approx tokens vs pi `1489` approx tokens = `7.9x`.
- V2 reduces Codex full fixed context from `11735` to `4483` approx tokens (`62%` reduction), but Codex's 10-tool schema payload (`4311` approx tokens) dominates after prompt/extra-block ablation.

## Notes

- The installed binary is `0.144.0`, not the expected `0.144.1`; all verdicts are for the installed binary that was actually probed.
- `include_apps_instructions` and `include_collaboration_mode_instructions` are verified as accepted and absent in V2 captures, but this specific CLI invocation did not emit positive app/collab blocks in V0/V1 to measure independently.
- Pi fixed context was captured through the CLI request path; `dist/core/system-prompt.js` matches the observed concise system prompt construction.
