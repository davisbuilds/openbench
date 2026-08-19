Gateway Probe
blocks cold=50/50 warm=50/50
retry evidence cells=500 attempts=506 first-attempt-successes=472 rescues=3 unknown-cost-attempts=31 budget-debit=$1.618285

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-openai | cold | 42/50 (84.0%; 95% CI 71.5%-91.7%) | 42/50 | 0.708s | 0.708s | 1.394s / 2.339s | 2.289s | 85.0 (42/42) | 0.0 (42/42) | $0.001500 (42/42) |
| cloudflare-openai | warm | 33/50 (66.0%; 95% CI 52.2%-77.6%) | 33/50 | 0.505s | 0.505s | 1.044s / 1.817s | 1.966s | 86.0 (33/33) | 0.0 (33/33) | $0.001510 (33/33) |
| concentrate-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.626s | 0.626s | 1.177s / 2.598s | 2.025s | 84.5 (50/50) | 0.0 (50/50) | $0.001495 (50/50) |
| concentrate-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.557s | 0.557s | 1.159s / 2.713s | 2.069s | 87.5 (50/50) | 0.0 (50/50) | $0.001557 (50/50) |
| direct-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.361s | 0.361s | 1.146s / 2.406s | 1.920s | 84.5 (50/50) | 0.0 (50/50) | $0.001470 (50/50) |
| direct-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.351s | 0.351s | 1.022s / 2.321s | 1.909s | 85.0 (50/50) | 0.0 (50/50) | $0.001488 (50/50) |
| openrouter-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.375s | 0.503s | 1.040s / 1.931s | 1.887s | 85.5 (50/50) | 0.0 (50/50) | $0.001508 (50/50) |
| openrouter-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.410s | 0.514s | 1.068s / 2.436s | 1.884s | 85.0 (50/50) | 0.0 (50/50) | $0.001510 (50/50) |
| vercel-openai | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.019s | 1.043s | 1.044s / 2.490s | 1.970s | 85.0 (50/50) | 0.0 (50/50) | $0.001502 (50/50) |
| vercel-openai | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.074s | 1.087s | 1.087s / 2.386s | 1.912s | 84.0 (50/50) | 0.0 (50/50) | $0.001468 (50/50) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-openai | 0.002s | 0.072s | 0.082s |
| concentrate-openai | 0.002s | 0.068s | 0.082s |
| direct-openai | 0.013s | 0.070s | 0.078s |
| openrouter-openai | 0.002s | 0.070s | 0.080s |
| vercel-openai | 0.003s | 0.071s | 0.089s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-openai | 0.847s | 0.851s | 1.541s | 2.477s |
| concentrate-openai | 0.814s | 0.814s | 1.391s | 2.292s |
| direct-openai | 0.598s | 0.599s | 1.310s | 2.113s |
| openrouter-openai | 0.565s | 0.672s | 1.261s | 2.120s |
| vercel-openai | 1.267s | 1.277s | 1.278s | 2.196s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-openai | cold | Setup DNS | -0.045s | 42/50 |
| cloudflare-openai | cold | Setup TCP | +0.001s | 42/50 |
| cloudflare-openai | cold | Setup TLS | +0.001s | 42/50 |
| cloudflare-openai | cold | Request to response headers | +0.307s | 42/50 |
| cloudflare-openai | cold | Request to first body byte | +0.307s | 42/50 |
| cloudflare-openai | cold | Request to semantic TTFT | +0.179s | 42/50 |
| cloudflare-openai | cold | Request stream total | +0.510s | 42/50 |
| cloudflare-openai | cold | Cold end-to-end response headers | +0.270s | 42/50 |
| cloudflare-openai | cold | Cold end-to-end first body byte | +0.270s | 42/50 |
| cloudflare-openai | cold | Cold end-to-end semantic TTFT | +0.160s | 42/50 |
| cloudflare-openai | cold | Cold end-to-end stream total | +0.296s | 42/50 |
| cloudflare-openai | warm | Request to response headers | +0.159s | 33/50 |
| cloudflare-openai | warm | Request to first body byte | +0.157s | 33/50 |
| cloudflare-openai | warm | Request to semantic TTFT | +0.116s | 33/50 |
| cloudflare-openai | warm | Request stream total | +0.069s | 33/50 |
| concentrate-openai | cold | Setup DNS | -0.011s | 50/50 |
| concentrate-openai | cold | Setup TCP | +0.000s | 50/50 |
| concentrate-openai | cold | Setup TLS | +0.005s | 50/50 |
| concentrate-openai | cold | Request to response headers | +0.234s | 50/50 |
| concentrate-openai | cold | Request to first body byte | +0.234s | 50/50 |
| concentrate-openai | cold | Request to semantic TTFT | +0.072s | 50/50 |
| concentrate-openai | cold | Request stream total | +0.219s | 50/50 |
| concentrate-openai | cold | Cold end-to-end response headers | +0.216s | 50/50 |
| concentrate-openai | cold | Cold end-to-end first body byte | +0.216s | 50/50 |
| concentrate-openai | cold | Cold end-to-end semantic TTFT | -0.015s | 50/50 |
| concentrate-openai | cold | Cold end-to-end stream total | +0.155s | 50/50 |
| concentrate-openai | warm | Request to response headers | +0.205s | 50/50 |
| concentrate-openai | warm | Request to first body byte | +0.205s | 50/50 |
| concentrate-openai | warm | Request to semantic TTFT | +0.162s | 50/50 |
| concentrate-openai | warm | Request stream total | +0.151s | 50/50 |
| openrouter-openai | cold | Setup DNS | -0.001s | 50/50 |
| openrouter-openai | cold | Setup TCP | -0.004s | 50/50 |
| openrouter-openai | cold | Setup TLS | -0.000s | 50/50 |
| openrouter-openai | cold | Request to response headers | +0.019s | 50/50 |
| openrouter-openai | cold | Request to first body byte | +0.155s | 50/50 |
| openrouter-openai | cold | Request to semantic TTFT | -0.139s | 50/50 |
| openrouter-openai | cold | Request stream total | -0.113s | 50/50 |
| openrouter-openai | cold | Cold end-to-end response headers | -0.011s | 50/50 |
| openrouter-openai | cold | Cold end-to-end first body byte | +0.094s | 50/50 |
| openrouter-openai | cold | Cold end-to-end semantic TTFT | -0.151s | 50/50 |
| openrouter-openai | cold | Cold end-to-end stream total | -0.156s | 50/50 |
| openrouter-openai | warm | Request to response headers | +0.061s | 50/50 |
| openrouter-openai | warm | Request to first body byte | +0.164s | 50/50 |
| openrouter-openai | warm | Request to semantic TTFT | +0.038s | 50/50 |
| openrouter-openai | warm | Request stream total | +0.067s | 50/50 |
| vercel-openai | cold | Setup DNS | +0.000s | 50/50 |
| vercel-openai | cold | Setup TCP | +0.005s | 50/50 |
| vercel-openai | cold | Setup TLS | +0.013s | 50/50 |
| vercel-openai | cold | Request to response headers | +0.613s | 50/50 |
| vercel-openai | cold | Request to first body byte | +0.631s | 50/50 |
| vercel-openai | cold | Request to semantic TTFT | +0.008s | 50/50 |
| vercel-openai | cold | Request stream total | +0.022s | 50/50 |
| vercel-openai | cold | Cold end-to-end response headers | +0.664s | 50/50 |
| vercel-openai | cold | Cold end-to-end first body byte | +0.669s | 50/50 |
| vercel-openai | cold | Cold end-to-end semantic TTFT | -0.059s | 50/50 |
| vercel-openai | cold | Cold end-to-end stream total | +0.021s | 50/50 |
| vercel-openai | warm | Request to response headers | +0.720s | 50/50 |
| vercel-openai | warm | Request to first body byte | +0.729s | 50/50 |
| vercel-openai | warm | Request to semantic TTFT | +0.088s | 50/50 |
| vercel-openai | warm | Request stream total | -0.012s | 50/50 |
