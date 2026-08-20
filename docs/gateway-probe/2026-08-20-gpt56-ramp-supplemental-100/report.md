Gateway Probe
blocks cold=50/50 warm=50/50
retry evidence cells=200 attempts=200 first-attempt-successes=200 rescues=0 unknown-cost-attempts=0 budget-debit=$0.447240

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| direct-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.492s | 0.492s | 1.131s / 2.016s | 2.049s | 84.0 (50/50) | 0.0 (50/50) | $0.001455 (50/50) |
| direct-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.419s | 0.426s | 1.038s / 1.924s | 1.924s | 84.0 (50/50) | 0.0 (50/50) | $0.001445 (50/50) |
| ramp-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.268s | 1.268s | 1.269s / 6.410s | 2.755s | 83.5 (50/50) | 0.0 (50/50) | $0.001427 (50/50) |
| ramp-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.692s | 1.693s | 1.693s / 6.233s | 2.947s | 83.0 (50/50) | 0.0 (50/50) | $0.001425 (50/50) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| direct-openai | 0.002s | 0.019s | 0.027s |
| ramp-openai | 0.002s | 0.020s | 0.028s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| direct-openai | 0.540s | 0.540s | 1.205s | 2.091s |
| ramp-openai | 1.331s | 1.331s | 1.332s | 2.803s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| ramp-openai | cold | Setup DNS | -0.000s | 50/50 |
| ramp-openai | cold | Setup TCP | +0.000s | 50/50 |
| ramp-openai | cold | Setup TLS | +0.002s | 50/50 |
| ramp-openai | cold | Request to response headers | +0.812s | 50/50 |
| ramp-openai | cold | Request to first body byte | +0.812s | 50/50 |
| ramp-openai | cold | Request to semantic TTFT | +0.254s | 50/50 |
| ramp-openai | cold | Request stream total | +0.825s | 50/50 |
| ramp-openai | cold | Cold end-to-end response headers | +0.816s | 50/50 |
| ramp-openai | cold | Cold end-to-end first body byte | +0.816s | 50/50 |
| ramp-openai | cold | Cold end-to-end semantic TTFT | +0.253s | 50/50 |
| ramp-openai | cold | Cold end-to-end stream total | +0.831s | 50/50 |
| ramp-openai | warm | Request to response headers | +1.295s | 50/50 |
| ramp-openai | warm | Request to first body byte | +1.295s | 50/50 |
| ramp-openai | warm | Request to semantic TTFT | +0.546s | 50/50 |
| ramp-openai | warm | Request stream total | +0.908s | 50/50 |
