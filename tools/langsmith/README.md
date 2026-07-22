# OpenBench LangSmith evaluation artifacts

These scripts convert OpenBench result rows into LangSmith examples and provide
local evaluators. They do not contact LangSmith.

## Build a dataset

```bash
python tools/langsmith/build_dataset.py \
  --results data/gpt56-2026-07-21/gpt56-final-7arms.jsonl \
  --tasks-dir tasks \
  --out results/langsmith-dataset.jsonl
```

Each JSONL line is an example with the LangSmith `inputs` and `outputs` keys,
plus OpenBench provenance under `metadata`. The file is suitable for
`langsmith dataset upload`:

```bash
langsmith dataset upload results/langsmith-dataset.jsonl \
  --name "OpenBench GPT-5.6 evaluation" \
  --api-key "$LANGSMITH_API_KEY"
```

The builder leaves `inputs.instruction` empty when a task's
`instruction.md` is missing.

## Evaluators

`evaluators.py` contains:

- `checker_verdict_agreement`: deterministic, uploadable custom code evaluator.
- `correctness_judge`, `root_cause_judge`, and `response_quality_judge`:
  local async LLM-as-judge evaluators. They use `langchain-openai` and
  structured output from `gpt-4o-mini`.

The offline replay runner verifies the deterministic evaluator without an API:

```bash
python tools/langsmith/run_eval.py results/langsmith-dataset.jsonl
```

For a real agent run, pass a run function and local evaluators to LangSmith's
`evaluate()` pattern:

```python
from langsmith import evaluate
from tools.langsmith.evaluators import correctness_judge, checker_verdict_agreement

results = evaluate(
    run_agent,
    data="OpenBench GPT-5.6 evaluation",
    evaluators=[checker_verdict_agreement, correctness_judge],
    experiment_prefix="openbench-eval-v1",
)
```

To upload the deterministic evaluator:

```bash
langsmith evaluator upload tools/langsmith/evaluators.py \
  --name "OpenBench checker verdict agreement" \
  --function checker_verdict_agreement \
  --dataset "OpenBench GPT-5.6 evaluation" \
  --api-key "$LANGSMITH_API_KEY"
```

## Environment and tracing

Dataset and evaluator commands require `LANGSMITH_API_KEY`; optionally set
`LANGSMITH_PROJECT` and `LANGSMITH_WORKSPACE_ID`. LLM-as-judge runs also need
`OPENAI_API_KEY` and:

```bash
pip install langsmith langchain-openai python-dotenv
```

Tracing an LLM application uses `LANGSMITH_TRACING=true`,
`LANGSMITH_API_KEY`, and optionally `LANGSMITH_PROJECT`. OpenBench harnesses
execute agents out-of-band, often in Docker, so LangSmith tracing is not wired
into the harnesses here. Local OpenBench transcripts remain the source for any
future trajectory dataset.
