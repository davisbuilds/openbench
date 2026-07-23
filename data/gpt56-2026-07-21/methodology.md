## Method

Six coding-agent CLI harnesses (cursor, grokbuild, opencode, claude-code, pi, codex) run the **identical model** — GPT-5.6-sol via ChatGPT/Codex subscription — on the **same 15 admission-gated tasks** (8 core software tasks + 7 imported Terminal-Bench tasks), **n=3 trials/cell**, **2400s timeout**, deterministic checkers, hack-adjusted scoring. All arms version-aligned across host and container (single pinned CLI version per harness; image_digest/version_source differences are rebuild + host-vs-container stamping artifacts, not drift).

**Correctness** is over *countable* cells — infra and rate-limited failures excluded (per-arm exclusion counts: claude 0, grokbuild 0, opencode 0, codex 1, pi 1, cursor 2). **Efficiency** columns are per-cell over the **all-solved intersection** (n=28 cells every harness solved), independently re-derived from raw data.

## Caveats (read before quoting)

- **Token basis differs.** cursor and opencode serve through protocols our counting proxy cannot meter → tokens are CLI-self-reported (`cli`). Do not rank cursor/opencode token cost against the proxy-metered arms (grokbuild, claude, pi, codex).
- **cursor** runs its own hosted GPT-5.6 deployment and reports no CLI version; the others hit the subscription endpoint. This is a harness-*product* comparison, not identical model serving.
- **n=45/arm** → correctness confidence intervals are wide (±~13pts); treat mid-table differences as ties. Wilson 95% CIs shown.
- Efficiency uses only the 28 cells every harness solved (all-solved intersection) — a comparability choice, not a random sample.
- **pi** is being rerun head-to-head on GPT-5.5 vs 5.6 across the hardest tasks to check a surprising per-task swing; its number here may update.
- Hardest tasks (extract-elf, feal, raman) are solved by few or no harnesses — the correctness spread is concentrated on this frontier tail.
