Gateway Probe
blocks cold=30/30 warm=30/30

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.739s | 2.739s | 2.739s / 5.709s | 6.780s | 261.5 (30/30) | 115.0 (25/30) | $0.002526 (30/30) |
| cloudflare-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.388s | 2.388s | 2.388s / 5.885s | 7.223s | 273.0 (30/30) | 115.0 (19/30) | $0.002715 (30/30) |
| concentrate-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.660s | 2.660s | 2.660s / 5.952s | 7.360s | 277.0 (30/30) | 114.5 (30/30) | $0.002781 (30/30) |
| concentrate-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.330s | 2.330s | 2.330s / 4.625s | 6.878s | 281.5 (30/30) | 115.0 (30/30) | $0.002806 (30/30) |
| direct-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.455s | 2.455s | 2.455s / 3.273s | 5.459s | 222.5 (30/30) | 115.0 (18/30) | $0.001941 (30/30) |
| direct-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.055s | 2.055s | 2.056s / 3.079s | 5.822s | 229.5 (30/30) | 115.0 (30/30) | $0.002069 (30/30) |
| openrouter-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.683s | 2.683s | 2.693s / 5.272s | 7.262s | 262.5 (30/30) | 0.0 (30/30) | $0.002581 (30/30) |
| openrouter-moonshot | warm | 29/30 (96.7%; 95% CI 83.3%-99.4%) | 29/30 | 2.735s | 2.735s | 2.774s / 4.789s | 7.187s | 246.0 (29/29) | 0.0 (29/29) | $0.002334 (29/29) |
| vercel-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.456s | 2.468s | 2.468s / 4.312s | 5.879s | 241.5 (30/30) | 115.0 (30/30) | $0.002236 (30/30) |
| vercel-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.403s | 2.407s | 2.407s / 4.663s | 6.451s | 231.0 (30/30) | 114.0 (30/30) | $0.002073 (30/30) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-moonshot | 0.001s | 0.011s | 0.014s |
| concentrate-moonshot | 0.001s | 0.011s | 0.016s |
| direct-moonshot | 0.001s | 0.011s | 0.017s |
| openrouter-moonshot | 0.001s | 0.011s | 0.015s |
| vercel-moonshot | 0.030s | 0.005s | 0.022s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-moonshot | 2.775s | 2.775s | 2.775s | 6.817s |
| concentrate-moonshot | 2.689s | 2.689s | 2.689s | 7.389s |
| direct-moonshot | 2.486s | 2.486s | 2.487s | 5.491s |
| openrouter-moonshot | 2.786s | 2.786s | 2.796s | 7.299s |
| vercel-moonshot | 2.532s | 2.540s | 2.540s | 5.951s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-moonshot | cold | Setup DNS | -0.000s | 30/30 |
| cloudflare-moonshot | cold | Setup TCP | +0.000s | 30/30 |
| cloudflare-moonshot | cold | Setup TLS | -0.003s | 30/30 |
| cloudflare-moonshot | cold | Request to response headers | +0.358s | 30/30 |
| cloudflare-moonshot | cold | Request to first body byte | +0.358s | 30/30 |
| cloudflare-moonshot | cold | Request to semantic TTFT | +0.358s | 30/30 |
| cloudflare-moonshot | cold | Request stream total | +1.684s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end response headers | +0.394s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end first body byte | +0.394s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end semantic TTFT | +0.394s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end stream total | +1.679s | 30/30 |
| cloudflare-moonshot | warm | Request to response headers | +0.120s | 30/30 |
| cloudflare-moonshot | warm | Request to first body byte | +0.120s | 30/30 |
| cloudflare-moonshot | warm | Request to semantic TTFT | +0.120s | 30/30 |
| cloudflare-moonshot | warm | Request stream total | +1.638s | 30/30 |
| concentrate-moonshot | cold | Setup DNS | -0.000s | 30/30 |
| concentrate-moonshot | cold | Setup TCP | +0.000s | 30/30 |
| concentrate-moonshot | cold | Setup TLS | -0.002s | 30/30 |
| concentrate-moonshot | cold | Request to response headers | +0.325s | 30/30 |
| concentrate-moonshot | cold | Request to first body byte | +0.325s | 30/30 |
| concentrate-moonshot | cold | Request to semantic TTFT | +0.324s | 30/30 |
| concentrate-moonshot | cold | Request stream total | +1.798s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end response headers | +0.323s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end first body byte | +0.323s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end semantic TTFT | +0.322s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end stream total | +1.795s | 30/30 |
| concentrate-moonshot | warm | Request to response headers | +0.146s | 30/30 |
| concentrate-moonshot | warm | Request to first body byte | +0.146s | 30/30 |
| concentrate-moonshot | warm | Request to semantic TTFT | +0.146s | 30/30 |
| concentrate-moonshot | warm | Request stream total | +0.701s | 30/30 |
| openrouter-moonshot | cold | Setup DNS | -0.000s | 30/30 |
| openrouter-moonshot | cold | Setup TCP | +0.000s | 30/30 |
| openrouter-moonshot | cold | Setup TLS | -0.003s | 30/30 |
| openrouter-moonshot | cold | Request to response headers | +0.387s | 30/30 |
| openrouter-moonshot | cold | Request to first body byte | +0.387s | 30/30 |
| openrouter-moonshot | cold | Request to semantic TTFT | +0.400s | 30/30 |
| openrouter-moonshot | cold | Request stream total | +1.980s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end response headers | +0.371s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end first body byte | +0.371s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end semantic TTFT | +0.385s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end stream total | +1.965s | 30/30 |
| openrouter-moonshot | warm | Request to response headers | +0.436s | 29/30 |
| openrouter-moonshot | warm | Request to first body byte | +0.436s | 29/30 |
| openrouter-moonshot | warm | Request to semantic TTFT | +0.450s | 29/30 |
| openrouter-moonshot | warm | Request stream total | +1.466s | 29/30 |
| vercel-moonshot | cold | Setup DNS | +0.028s | 30/30 |
| vercel-moonshot | cold | Setup TCP | -0.006s | 30/30 |
| vercel-moonshot | cold | Setup TLS | +0.005s | 30/30 |
| vercel-moonshot | cold | Request to response headers | +0.077s | 30/30 |
| vercel-moonshot | cold | Request to first body byte | +0.085s | 30/30 |
| vercel-moonshot | cold | Request to semantic TTFT | +0.085s | 30/30 |
| vercel-moonshot | cold | Request stream total | +0.082s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end response headers | +0.123s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end first body byte | +0.132s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end semantic TTFT | +0.132s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end stream total | +0.114s | 30/30 |
| vercel-moonshot | warm | Request to response headers | +0.248s | 30/30 |
| vercel-moonshot | warm | Request to first body byte | +0.259s | 30/30 |
| vercel-moonshot | warm | Request to semantic TTFT | +0.259s | 30/30 |
| vercel-moonshot | warm | Request stream total | +0.960s | 30/30 |
