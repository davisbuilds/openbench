Gateway Probe
blocks cold=50/50 warm=50/50
retry evidence cells=400 attempts=400 first-attempt-successes=400 rescues=0 unknown-cost-attempts=0 budget-debit=$0.895050

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| concentrate-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.665s | 0.665s | 1.326s / 2.691s | 2.272s | 85.5 (50/50) | 0.0 (50/50) | $0.001495 (50/50) |
| concentrate-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.491s | 0.491s | 1.082s / 2.328s | 1.949s | 85.0 (50/50) | 0.0 (50/50) | $0.001445 (50/50) |
| direct-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.401s | 0.401s | 0.996s / 2.612s | 1.907s | 84.0 (50/50) | 0.0 (50/50) | $0.001420 (50/50) |
| direct-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.341s | 0.341s | 0.981s / 1.747s | 1.940s | 87.0 (50/50) | 0.0 (50/50) | $0.001502 (50/50) |
| openrouter-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.423s | 0.528s | 1.019s / 2.090s | 1.907s | 83.5 (50/50) | 0.0 (50/50) | $0.001463 (50/50) |
| openrouter-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.367s | 0.466s | 0.955s / 2.617s | 1.838s | 85.0 (50/50) | 0.0 (50/50) | $0.001448 (50/50) |
| vercel-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.116s | 1.126s | 1.127s / 2.254s | 2.037s | 86.0 (50/50) | 0.0 (50/50) | $0.001510 (50/50) |
| vercel-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.985s | 0.995s | 0.995s / 2.502s | 1.931s | 87.0 (50/50) | 0.0 (50/50) | $0.001522 (50/50) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| concentrate-openai | 0.002s | 0.020s | 0.028s |
| direct-openai | 0.002s | 0.019s | 0.026s |
| openrouter-openai | 0.002s | 0.020s | 0.026s |
| vercel-openai | 0.003s | 0.018s | 0.037s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| concentrate-openai | 0.717s | 0.718s | 1.375s | 2.321s |
| direct-openai | 0.463s | 0.463s | 1.061s | 1.962s |
| openrouter-openai | 0.472s | 0.579s | 1.073s | 1.962s |
| vercel-openai | 1.201s | 1.211s | 1.212s | 2.097s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| concentrate-openai | cold | Setup DNS | -0.001s | 50/50 |
| concentrate-openai | cold | Setup TCP | -0.000s | 50/50 |
| concentrate-openai | cold | Setup TLS | +0.002s | 50/50 |
| concentrate-openai | cold | Request to response headers | +0.252s | 50/50 |
| concentrate-openai | cold | Request to first body byte | +0.252s | 50/50 |
| concentrate-openai | cold | Request to semantic TTFT | +0.348s | 50/50 |
| concentrate-openai | cold | Request stream total | +0.330s | 50/50 |
| concentrate-openai | cold | Cold end-to-end response headers | +0.247s | 50/50 |
| concentrate-openai | cold | Cold end-to-end first body byte | +0.247s | 50/50 |
| concentrate-openai | cold | Cold end-to-end semantic TTFT | +0.330s | 50/50 |
| concentrate-openai | cold | Cold end-to-end stream total | +0.316s | 50/50 |
| concentrate-openai | warm | Request to response headers | +0.153s | 50/50 |
| concentrate-openai | warm | Request to first body byte | +0.152s | 50/50 |
| concentrate-openai | warm | Request to semantic TTFT | +0.097s | 50/50 |
| concentrate-openai | warm | Request stream total | +0.185s | 50/50 |
| openrouter-openai | cold | Setup DNS | -0.000s | 50/50 |
| openrouter-openai | cold | Setup TCP | +0.000s | 50/50 |
| openrouter-openai | cold | Setup TLS | +0.000s | 50/50 |
| openrouter-openai | cold | Request to response headers | +0.030s | 50/50 |
| openrouter-openai | cold | Request to first body byte | +0.135s | 50/50 |
| openrouter-openai | cold | Request to semantic TTFT | +0.044s | 50/50 |
| openrouter-openai | cold | Request stream total | +0.053s | 50/50 |
| openrouter-openai | cold | Cold end-to-end response headers | +0.025s | 50/50 |
| openrouter-openai | cold | Cold end-to-end first body byte | +0.126s | 50/50 |
| openrouter-openai | cold | Cold end-to-end semantic TTFT | +0.041s | 50/50 |
| openrouter-openai | cold | Cold end-to-end stream total | +0.049s | 50/50 |
| openrouter-openai | warm | Request to response headers | +0.031s | 50/50 |
| openrouter-openai | warm | Request to first body byte | +0.130s | 50/50 |
| openrouter-openai | warm | Request to semantic TTFT | +0.078s | 50/50 |
| openrouter-openai | warm | Request stream total | +0.017s | 50/50 |
| vercel-openai | cold | Setup DNS | +0.000s | 50/50 |
| vercel-openai | cold | Setup TCP | -0.002s | 50/50 |
| vercel-openai | cold | Setup TLS | +0.011s | 50/50 |
| vercel-openai | cold | Request to response headers | +0.624s | 50/50 |
| vercel-openai | cold | Request to first body byte | +0.630s | 50/50 |
| vercel-openai | cold | Request to semantic TTFT | +0.084s | 50/50 |
| vercel-openai | cold | Request stream total | +0.243s | 50/50 |
| vercel-openai | cold | Cold end-to-end response headers | +0.639s | 50/50 |
| vercel-openai | cold | Cold end-to-end first body byte | +0.643s | 50/50 |
| vercel-openai | cold | Cold end-to-end semantic TTFT | +0.112s | 50/50 |
| vercel-openai | cold | Cold end-to-end stream total | +0.241s | 50/50 |
| vercel-openai | warm | Request to response headers | +0.637s | 50/50 |
| vercel-openai | warm | Request to first body byte | +0.642s | 50/50 |
| vercel-openai | warm | Request to semantic TTFT | +0.126s | 50/50 |
| vercel-openai | warm | Request stream total | +0.067s | 50/50 |
