# Session evidence repair contract

This supplement clarifies the public boundary for full historical checkouts.
It contains synthetic input examples and public regression tests, not a repair.
Copy `test_repair_contract.py` into the new case's `environment/app/tests/` and
append this contract to its instruction. Advance the case revision; leave earlier
packages and sealed runs unchanged. Preserve all original project files.

## Recorded listings

A native instruction record has this shape:

```json
{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"<skills_instructions>...</skills_instructions>"}]}}
```

`output_text` content is also supported. Listings in these developer messages
must be recognized. User, assistant and tool messages, and compaction summaries,
are not authoritative listings, even if they contain identical markup.

The supplied historical public fixtures also use
`{"type":"turn_context","payload":{"text":"<skills_instructions>...</skills_instructions>"}}`.
Some older public tests use `type="turn"` with the same direct `payload.text`.
Retain support for those direct legacy fields; do not recursively search arbitrary
nested fields for matching text. A listing in a native developer message takes
precedence over a legacy fixture listing. Distinguish no listing (`None`) from
an observed empty listing (an observation with zero entries).

## Invocation mode

The full historical source predates the mode-aware Python interface used by this
repair task. Extend `budget.assess` with an optional keyword-only `surface=None`
and `budget.Assessment` with an optional `surface=None` constructor argument.
Keep existing arguments and return shapes compatible. Both the normal assess
path and directly constructed assessments must apply the policy's declared-mode
rules. A missing or undeclared mode cannot be deployable or gate a build.

These are caller-visible interface requirements, not a required internal
algorithm. Continue to support the existing listing-comparison interface and
return shapes described in the task.

Run the public examples with:

```sh
python3 -m pytest -q tests/test_repair_contract.py
```

They expose the supported formats and API boundary; they are not exhaustive
acceptance coverage. Existing pre-fix tests may assert behavior this task changes.
Review such assertions against the contract rather than preserving the defect.
