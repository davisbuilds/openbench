Gateway Probe
blocks cold=50/50 warm=50/50
retry evidence cells=500 attempts=500 first-attempt-successes=500 rescues=0 unknown-cost-attempts=0 budget-debit=$1.843545

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-moonshot | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.831s | 2.831s | 2.831s / 5.256s | 6.788s | 227.5 (50/50) | 116.0 (30/50) | $0.002033 (50/50) |
| cloudflare-moonshot | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.344s | 2.344s | 2.344s / 4.020s | 5.966s | 237.0 (50/50) | 116.0 (42/50) | $0.002157 (50/50) |
| concentrate-moonshot | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.699s | 2.699s | 2.699s / 3.839s | 5.707s | 224.0 (50/50) | 114.0 (50/50) | $0.001974 (50/50) |
| concentrate-moonshot | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.513s | 2.514s | 2.514s / 3.691s | 6.677s | 251.5 (50/50) | 115.0 (50/50) | $0.002361 (50/50) |
| direct-moonshot | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.256s | 2.256s | 2.257s / 3.493s | 6.932s | 263.0 (50/50) | 115.0 (50/50) | $0.002559 (50/50) |
| direct-moonshot | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.350s | 2.351s | 2.351s / 3.710s | 6.798s | 256.0 (50/50) | 115.0 (30/50) | $0.002424 (50/50) |
| openrouter-moonshot | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.537s | 2.538s | 2.568s / 3.392s | 6.011s | 237.0 (50/50) | 0.0 (50/50) | $0.002169 (50/50) |
| openrouter-moonshot | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.423s | 2.423s | 2.436s / 2.988s | 6.580s | 260.5 (50/50) | 0.0 (50/50) | $0.002517 (50/50) |
| vercel-moonshot | cold | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.726s | 2.734s | 2.734s / 4.504s | 6.541s | 244.5 (50/50) | 113.0 (50/50) | $0.002316 (50/50) |
| vercel-moonshot | warm | 50/50 (100.0%; 95% CI 92.9%-100.0%) | 50/50 | 2.459s | 2.459s | 2.459s / 3.138s | 6.320s | 252.0 (50/50) | 115.0 (50/50) | $0.002388 (50/50) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-moonshot | 0.002s | 0.011s | 0.016s |
| concentrate-moonshot | 0.001s | 0.011s | 0.018s |
| direct-moonshot | 0.002s | 0.012s | 0.018s |
| openrouter-moonshot | 0.001s | 0.011s | 0.016s |
| vercel-moonshot | 0.002s | 0.005s | 0.024s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-moonshot | 2.861s | 2.861s | 2.861s | 6.841s |
| concentrate-moonshot | 2.730s | 2.730s | 2.730s | 5.737s |
| direct-moonshot | 2.341s | 2.341s | 2.341s | 6.963s |
| openrouter-moonshot | 2.593s | 2.593s | 2.631s | 6.041s |
| vercel-moonshot | 2.781s | 2.790s | 2.790s | 6.574s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-moonshot | cold | Setup DNS | -0.000s | 50/50 |
| cloudflare-moonshot | cold | Setup TCP | -0.000s | 50/50 |
| cloudflare-moonshot | cold | Setup TLS | -0.003s | 50/50 |
| cloudflare-moonshot | cold | Request to response headers | +0.413s | 50/50 |
| cloudflare-moonshot | cold | Request to first body byte | +0.413s | 50/50 |
| cloudflare-moonshot | cold | Request to semantic TTFT | +0.412s | 50/50 |
| cloudflare-moonshot | cold | Request stream total | -0.184s | 50/50 |
| cloudflare-moonshot | cold | Cold end-to-end response headers | +0.408s | 50/50 |
| cloudflare-moonshot | cold | Cold end-to-end first body byte | +0.408s | 50/50 |
| cloudflare-moonshot | cold | Cold end-to-end semantic TTFT | +0.408s | 50/50 |
| cloudflare-moonshot | cold | Cold end-to-end stream total | -0.187s | 50/50 |
| cloudflare-moonshot | warm | Request to response headers | +0.067s | 50/50 |
| cloudflare-moonshot | warm | Request to first body byte | +0.067s | 50/50 |
| cloudflare-moonshot | warm | Request to semantic TTFT | +0.067s | 50/50 |
| cloudflare-moonshot | warm | Request stream total | -0.320s | 50/50 |
| concentrate-moonshot | cold | Setup DNS | -0.000s | 50/50 |
| concentrate-moonshot | cold | Setup TCP | -0.000s | 50/50 |
| concentrate-moonshot | cold | Setup TLS | -0.000s | 50/50 |
| concentrate-moonshot | cold | Request to response headers | +0.249s | 50/50 |
| concentrate-moonshot | cold | Request to first body byte | +0.251s | 50/50 |
| concentrate-moonshot | cold | Request to semantic TTFT | +0.251s | 50/50 |
| concentrate-moonshot | cold | Request stream total | +0.025s | 50/50 |
| concentrate-moonshot | cold | Cold end-to-end response headers | +0.209s | 50/50 |
| concentrate-moonshot | cold | Cold end-to-end first body byte | +0.209s | 50/50 |
| concentrate-moonshot | cold | Cold end-to-end semantic TTFT | +0.209s | 50/50 |
| concentrate-moonshot | cold | Cold end-to-end stream total | -0.026s | 50/50 |
| concentrate-moonshot | warm | Request to response headers | +0.236s | 50/50 |
| concentrate-moonshot | warm | Request to first body byte | +0.236s | 50/50 |
| concentrate-moonshot | warm | Request to semantic TTFT | +0.236s | 50/50 |
| concentrate-moonshot | warm | Request stream total | +0.393s | 50/50 |
| openrouter-moonshot | cold | Setup DNS | -0.000s | 50/50 |
| openrouter-moonshot | cold | Setup TCP | -0.000s | 50/50 |
| openrouter-moonshot | cold | Setup TLS | -0.002s | 50/50 |
| openrouter-moonshot | cold | Request to response headers | +0.259s | 50/50 |
| openrouter-moonshot | cold | Request to first body byte | +0.259s | 50/50 |
| openrouter-moonshot | cold | Request to semantic TTFT | +0.271s | 50/50 |
| openrouter-moonshot | cold | Request stream total | -0.509s | 50/50 |
| openrouter-moonshot | cold | Cold end-to-end response headers | +0.233s | 50/50 |
| openrouter-moonshot | cold | Cold end-to-end first body byte | +0.233s | 50/50 |
| openrouter-moonshot | cold | Cold end-to-end semantic TTFT | +0.242s | 50/50 |
| openrouter-moonshot | cold | Cold end-to-end stream total | -0.495s | 50/50 |
| openrouter-moonshot | warm | Request to response headers | +0.187s | 50/50 |
| openrouter-moonshot | warm | Request to first body byte | +0.187s | 50/50 |
| openrouter-moonshot | warm | Request to semantic TTFT | +0.193s | 50/50 |
| openrouter-moonshot | warm | Request stream total | -0.303s | 50/50 |
| vercel-moonshot | cold | Setup DNS | +0.000s | 50/50 |
| vercel-moonshot | cold | Setup TCP | -0.006s | 50/50 |
| vercel-moonshot | cold | Setup TLS | +0.005s | 50/50 |
| vercel-moonshot | cold | Request to response headers | +0.297s | 50/50 |
| vercel-moonshot | cold | Request to first body byte | +0.306s | 50/50 |
| vercel-moonshot | cold | Request to semantic TTFT | +0.306s | 50/50 |
| vercel-moonshot | cold | Request stream total | -0.134s | 50/50 |
| vercel-moonshot | cold | Cold end-to-end response headers | +0.297s | 50/50 |
| vercel-moonshot | cold | Cold end-to-end first body byte | +0.306s | 50/50 |
| vercel-moonshot | cold | Cold end-to-end semantic TTFT | +0.306s | 50/50 |
| vercel-moonshot | cold | Cold end-to-end stream total | -0.129s | 50/50 |
| vercel-moonshot | warm | Request to response headers | +0.117s | 50/50 |
| vercel-moonshot | warm | Request to first body byte | +0.122s | 50/50 |
| vercel-moonshot | warm | Request to semantic TTFT | +0.122s | 50/50 |
| vercel-moonshot | warm | Request stream total | +0.336s | 50/50 |
