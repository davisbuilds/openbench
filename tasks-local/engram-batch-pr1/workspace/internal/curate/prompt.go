package curate

// promptHeader frames the task; promptContract pins the exact output schema. The
// corpus JSON is spliced between them by BuildPrompt. The contract is
// deliberately strict: engram parses only what it can validate, and applies only
// a fully-valid batch, so an off-schema or hedged response is safely rejected
// rather than half-applied.
const promptHeader = `You are curating a canonical agent-memory store for engram.

Canonical memories are the single source of truth; engram renders them into each
harness. Each memory is a markdown file with YAML frontmatter. Required fields:
name (kebab-case), description (one line), type (one of: user, feedback, project,
reference, lesson, preference), scope (either "global" or "project:<repo>").
Optional: applies_to {cwd[], agents[], hosts[]}, related[], provenance, and a
markdown body.

Your job: read the whole corpus below and propose consolidation operations that
make it cleaner and more useful — merge true duplicates, remove stale or
redundant memories, fix over-narrow or over-broad scopes, and tighten wording.
Be conservative: propose an operation only when you are confident it improves the
store. It is correct to propose nothing.`

const promptContract = `## Output contract

Respond with ONLY a single fenced JSON block — no prose before or after it:

` + "```json" + `
{
  "operations": [
    { "op": "merge", "sources": ["name-a", "name-b"],
      "memory": { "name": "...", "description": "...", "type": "...", "scope": "...", "body": "..." },
      "reason": "why these are the same memory" },
    { "op": "remove", "name": "stale-name", "reason": "why it is safe to delete" },
    { "op": "rescope", "name": "some-name", "to_scope": "global", "reason": "why the scope should change" },
    { "op": "update", "name": "some-name",
      "memory": { "name": "some-name", "description": "...", "type": "...", "scope": "...", "body": "..." },
      "reason": "what you tightened" },
    { "op": "add", "memory": { "name": "new-name", "description": "...", "type": "...", "scope": "...", "body": "..." },
      "reason": "why this belongs in the store" }
  ]
}
` + "```" + `

Rules:
- For "update" the memory.name MUST equal the target name (renames are not updates).
- For "merge" list at least two existing sources; the merged "memory" replaces
  them (a source whose name equals memory.name is kept, the others are deleted).
- For "add" the memory.name must not already exist.
- Every "memory" you emit must satisfy the required-field contract above.
- If nothing should change, return {"operations": []}.
- Do not invent memories, hosts, or scopes that are not supported.`
