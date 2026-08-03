Gateway Probe
blocks cold=50/50 warm=50/50
retry evidence cells=500 attempts=500 first-attempt-successes=500 rescues=0 unknown-cost-attempts=0 budget-debit=$0.018596

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-deepseek | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.022s | 1.022s | 1.022s / 4.238s | 1.619s | 83.5 (50/50) | 0.0 (50/50) | $0.000018 (50/50) |
| cloudflare-deepseek | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 1.002s | 1.002s | 1.002s / 2.793s | 1.665s | 82.0 (50/50) | 0.0 (50/50) | $0.000018 (50/50) |
| concentrate-deepseek | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.558s | 0.558s | 1.105s / 1.539s | 1.491s | 148.0 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| concentrate-deepseek | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.478s | 0.478s | 1.036s / 1.262s | 1.426s | 148.0 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| direct-deepseek | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.345s | 0.356s | 0.853s / 1.144s | 1.272s | 149.5 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| direct-deepseek | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.338s | 0.346s | 0.864s / 1.153s | 1.274s | 147.5 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| openrouter-deepseek | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.446s | 0.522s | 0.978s / 1.212s | 1.418s | 147.5 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| openrouter-deepseek | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.379s | 0.462s | 0.917s / 1.172s | 1.390s | 149.0 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| vercel-deepseek | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.502s | 0.513s | 1.563s / 6.838s | 1.960s | 149.0 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |
| vercel-deepseek | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 0.428s | 0.437s | 1.328s / 8.706s | 1.786s | 147.5 (50/50) | 0.0 (50/50) | $0.000025 (50/50) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-deepseek | 0.001s | 0.012s | 0.016s |
| concentrate-deepseek | 0.001s | 0.012s | 0.017s |
| direct-deepseek | 0.002s | 0.006s | 0.009s |
| openrouter-deepseek | 0.001s | 0.011s | 0.015s |
| vercel-deepseek | 0.002s | 0.006s | 0.023s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-deepseek | 1.078s | 1.078s | 1.078s | 1.659s |
| concentrate-deepseek | 0.588s | 0.588s | 1.133s | 1.519s |
| direct-deepseek | 0.371s | 0.379s | 0.876s | 1.299s |
| openrouter-deepseek | 0.479s | 0.554s | 1.010s | 1.445s |
| vercel-deepseek | 0.541s | 0.545s | 1.595s | 2.005s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-deepseek | cold | Setup DNS | -0.000s | 50/50 |
| cloudflare-deepseek | cold | Setup TCP | +0.005s | 50/50 |
| cloudflare-deepseek | cold | Setup TLS | +0.006s | 50/50 |
| cloudflare-deepseek | cold | Request to response headers | +0.676s | 50/50 |
| cloudflare-deepseek | cold | Request to first body byte | +0.673s | 50/50 |
| cloudflare-deepseek | cold | Request to semantic TTFT | +0.145s | 50/50 |
| cloudflare-deepseek | cold | Request stream total | +0.221s | 50/50 |
| cloudflare-deepseek | cold | Cold end-to-end response headers | +0.675s | 50/50 |
| cloudflare-deepseek | cold | Cold end-to-end first body byte | +0.669s | 50/50 |
| cloudflare-deepseek | cold | Cold end-to-end semantic TTFT | +0.158s | 50/50 |
| cloudflare-deepseek | cold | Cold end-to-end stream total | +0.231s | 50/50 |
| cloudflare-deepseek | warm | Request to response headers | +0.668s | 50/50 |
| cloudflare-deepseek | warm | Request to first body byte | +0.660s | 50/50 |
| cloudflare-deepseek | warm | Request to semantic TTFT | +0.168s | 50/50 |
| cloudflare-deepseek | warm | Request stream total | +0.374s | 50/50 |
| concentrate-deepseek | cold | Setup DNS | -0.001s | 50/50 |
| concentrate-deepseek | cold | Setup TCP | +0.005s | 50/50 |
| concentrate-deepseek | cold | Setup TLS | +0.008s | 50/50 |
| concentrate-deepseek | cold | Request to response headers | +0.203s | 50/50 |
| concentrate-deepseek | cold | Request to first body byte | +0.198s | 50/50 |
| concentrate-deepseek | cold | Request to semantic TTFT | +0.248s | 50/50 |
| concentrate-deepseek | cold | Request stream total | +0.249s | 50/50 |
| concentrate-deepseek | cold | Cold end-to-end response headers | +0.205s | 50/50 |
| concentrate-deepseek | cold | Cold end-to-end first body byte | +0.198s | 50/50 |
| concentrate-deepseek | cold | Cold end-to-end semantic TTFT | +0.241s | 50/50 |
| concentrate-deepseek | cold | Cold end-to-end stream total | +0.240s | 50/50 |
| concentrate-deepseek | warm | Request to response headers | +0.143s | 50/50 |
| concentrate-deepseek | warm | Request to first body byte | +0.136s | 50/50 |
| concentrate-deepseek | warm | Request to semantic TTFT | +0.172s | 50/50 |
| concentrate-deepseek | warm | Request stream total | +0.159s | 50/50 |
| openrouter-deepseek | cold | Setup DNS | -0.001s | 50/50 |
| openrouter-deepseek | cold | Setup TCP | +0.005s | 50/50 |
| openrouter-deepseek | cold | Setup TLS | +0.006s | 50/50 |
| openrouter-deepseek | cold | Request to response headers | +0.098s | 50/50 |
| openrouter-deepseek | cold | Request to first body byte | +0.164s | 50/50 |
| openrouter-deepseek | cold | Request to semantic TTFT | +0.129s | 50/50 |
| openrouter-deepseek | cold | Request stream total | +0.169s | 50/50 |
| openrouter-deepseek | cold | Cold end-to-end response headers | +0.109s | 50/50 |
| openrouter-deepseek | cold | Cold end-to-end first body byte | +0.171s | 50/50 |
| openrouter-deepseek | cold | Cold end-to-end semantic TTFT | +0.136s | 50/50 |
| openrouter-deepseek | cold | Cold end-to-end stream total | +0.161s | 50/50 |
| openrouter-deepseek | warm | Request to response headers | +0.038s | 50/50 |
| openrouter-deepseek | warm | Request to first body byte | +0.117s | 50/50 |
| openrouter-deepseek | warm | Request to semantic TTFT | +0.058s | 50/50 |
| openrouter-deepseek | warm | Request stream total | +0.128s | 50/50 |
| vercel-deepseek | cold | Setup DNS | -0.000s | 50/50 |
| vercel-deepseek | cold | Setup TCP | -0.001s | 50/50 |
| vercel-deepseek | cold | Setup TLS | +0.014s | 50/50 |
| vercel-deepseek | cold | Request to response headers | +0.132s | 50/50 |
| vercel-deepseek | cold | Request to first body byte | +0.135s | 50/50 |
| vercel-deepseek | cold | Request to semantic TTFT | +0.712s | 50/50 |
| vercel-deepseek | cold | Request stream total | +0.694s | 50/50 |
| vercel-deepseek | cold | Cold end-to-end response headers | +0.153s | 50/50 |
| vercel-deepseek | cold | Cold end-to-end first body byte | +0.153s | 50/50 |
| vercel-deepseek | cold | Cold end-to-end semantic TTFT | +0.726s | 50/50 |
| vercel-deepseek | cold | Cold end-to-end stream total | +0.703s | 50/50 |
| vercel-deepseek | warm | Request to response headers | +0.088s | 50/50 |
| vercel-deepseek | warm | Request to first body byte | +0.087s | 50/50 |
| vercel-deepseek | warm | Request to semantic TTFT | +0.445s | 50/50 |
| vercel-deepseek | warm | Request stream total | +0.509s | 50/50 |
