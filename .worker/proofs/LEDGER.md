# Token parity probe ledger

Goal: capture pi/opencode/claude/codex token usage streams for deepseek-v4-flash, compare against LiteLLM bridge vendor usage on :4242, and document normalization design.

## Attempts

## Attempt 1
Started separate LiteLLM bridge on 127.0.0.1:4242 with --detailed_debug; pid 64388.

## Attempt 2
Created per-harness probe runner using isolated work dirs/HOMEs and bridge :4242.

## Attempt 3
Ran initial probes for pi/opencode/claude/codex; see fixtures and results inspected in terminal.

## Attempt 4
Restarted bridge on :4242 with custom usage logger callback for vendor usage JSONL.

## Attempt 4
Restarted bridge on :4242 with custom usage logger callback for vendor usage JSONL.

## Attempt 5
Fixed callback import by copying hooks.py beside probe config and restarted bridge.

## Attempt 6
Reran all four probes against :4242 with custom bridge usage logger active; overwrote raw stream fixtures with latest captures.

## Attempt 7
Captured direct DeepSeek usage schema sample in .worker/proofs/logs/deepseek-direct-usage.json (no key/header).

## Attempt 8
Created cleaned unit-test fixtures under bench/tests/fixtures/usage/deepseek-v4-flash and bridge usage excerpt.

## Attempt 9
Self-test: parity_probe --help; parser assertions for raw and clean fixtures; codex bridge excerpt comparison zero-diff.

## Attempt 10
Cleanup: killed the probe LiteLLM bridge on :4242 and verified the port is closed.

## Attempt 12
Adjusted parity_probe Claude parser to represent unavailable reasoning as null and regenerated summaries.

## Attempt 13
Closeout self-test: py_compile; parity_probe help; raw+clean fixture parser assertions; codex bridge zero-diff; secret scan; :4242 closed.

## Attempt 14
Simplify/cleanup: adjusted parity_probe Claude parser to prefer modelUsage cumulative totals, matching TOKEN_PARITY.md design; reran parser assertions and codex bridge zero-diff.

## Attempt 15
Autoreview found UsageLogger serialized full responses. Fixed .worker/proofs/usage_logger.py to log only usage plus non-sensitive metadata (finish_reason/has_tool_calls), never choices/messages/tool args.

## Attempt 16
Focused re-test after UsageLogger privacy fix: py_compile, parity_probe help, parser assertions, codex bridge zero-diff, sensitive-field scan, :4242 closed.

## Attempt 17
Addressed autoreview: validated run_probe harness before rm -rf; minimized bench/tests usage fixtures to usage-only records with prompt/output/reasoning text removed. Raw full stdout remains only in .worker/proofs/fixtures per delegated proof requirement.

## Attempt 18
Post-autoreview-fix self-test: py_compile, parity_probe help, raw+clean parser assertions, codex bridge zero-diff, sensitive-field scan, :4242 closed, staged exact key scan.

## Attempt 19
Addressed autoreview: parity_probe now returns nonzero on failed --run and matches bridge records by CLI completion-token sequence for pi/opencode shared logs, avoiding background/title-call contamination. Re-tested pi/opencode/codex zero-diff.

## Attempt 20
Addressed autoreview privacy finding: replaced staged .worker/proofs/fixtures streams with usage-only scrubbed records matching clean fixtures; TOKEN_PARITY now describes scrubbed captured usage streams rather than raw reasoning transcripts.

## Attempt 22
Final focused self-test: py_compile, parity_probe help, proof+test parser assertions, pi/opencode/codex bridge zero-diff, scrubbed transcript scan, :4242 closed, staged exact key scan.

## Attempt 23
Addressed autoreview Claude parser finding: parse single JSON objects defensively from noisy streams and return structured error on parse/run failure. Re-tested py_compile, parser assertions, bridge zero-diff, noisy Claude parse, scrubbed scan, :4242 closed.

## Attempt 24
Addressed autoreview empty-stream finding: JSONL parsers now raise when no usage-bearing records are found. Re-tested py_compile, help, parser assertions, zero-diff comparisons, noisy Claude parse, empty-stream rejection, scrubbed scan, :4242 closed.

## Attempt 25
Addressed autoreview missing-payload finding: JSONL parsers now require expected usage fields, not just event type. Re-tested py_compile, help, parser assertions, bridge zero-diff, noisy Claude parse, empty/missing-payload rejection, scrubbed scan, :4242 closed.

## Attempt 26
Addressed autoreview filename-only log path finding: UsageLogger now creates parent dirs only when dirname is non-empty. Re-tested py_compile, filename-only callback write, parity_probe help, parser assertions, bridge zero-diff, noisy Claude parse, empty/missing rejection, scrubbed scan, :4242 closed.

## Attempt 27
Addressed autoreview Claude unknown reasoning diff: diff now preserves null/unknown instead of coercing to zero; verified Claude bridge comparison has tokens_reasoning=null and I/O zero-diff.

## Attempt 28
Addressed autoreview required-field and empty-bridge findings: parsers require complete split fields; bridge parser raises on empty filters. Re-tested py_compile, help, fixture assertions, all bridge comparisons, noisy Claude, incomplete stream rejection, empty bridge rejection, scrubbed scan, :4242 closed.

## Attempt 29
Addressed autoreview incomplete bridge usage finding: bridge parser now requires usage/detail fields and rejects malformed bridge records. Re-tested full focused suite including incomplete bridge rejection.

## Attempt 30
Addressed autoreview env-scrub finding: parity_probe and run_probe now launch agent CLIs with minimal env/placeholder key only. Re-tested py_compile, bash -n, help, fixture assertions, bridge comparisons, defensive parser checks, scrubbed scan, :4242 closed.

## Attempt 31
Addressed autoreview SSH agent exposure: parity_probe child env allowlist now excludes SSH_AUTH_SOCK/XDG_RUNTIME_DIR; run_probe already uses env -i without SSH agent.

## Attempt 32
Addressed autoreview optional bridge-call-type finding: parity_probe now defaults bridge call_type from harness (pi/opencode=acompletion, claude=anthropic_messages, codex=aresponses) and records it in output.
