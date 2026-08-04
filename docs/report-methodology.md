## Method

- Denominators are countable cells. Infrastructure and rate-limit failures are excluded; other failures, including timeouts, remain in the denominator.
- Correctness uses the canonical OpenBench aggregation and Wilson 95% confidence intervals. Where timeouts occur, the report shows both accuracy at the configured cap and accuracy among finished cells.
- Efficiency is descriptive: median wall time among solved cells and total measured tokens divided by solves. Usage evidence labels distinguish Harbor-reported values, proxy-verified reconciliation, and mismatches. Mismatched usage is excluded from token, cost, and efficiency metrics while correctness and latency remain publishable.
- Harness defaults are deliberately not clamped because they are part of the product being evaluated. Sampling and provenance should be inspected in the source JSONL.

## Limitations

- Results cover only the included tasks, trials, model deployments, harness versions, and timeout caps; they do not establish a universal ranking.
- Mixed versions or caps, incomplete cells, and differing token protocols can weaken comparability. CLI-reported and proxy-metered token values should not be treated as interchangeable.
- Cost appears only for models with a configured price and includes uncached input and output according to the canonical OpenBench price calculation; subscription pricing and cache-specific pricing are not inferred.
- Long-horizon work, multi-agent modes, and unsupported model/harness combinations may not be represented.
