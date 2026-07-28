Gateway Probe
blocks cold=30/30 warm=30/30

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.352s | 2.352s | 2.352s / 3.247s | 4.422s | 223.5 (30/30) | 115.0 (24/30) | $0.001979 (30/30) |
| cloudflare-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 1.911s | 1.911s | 1.911s / 2.432s | 4.378s | 241.0 (30/30) | 115.0 (24/30) | $0.002247 (30/30) |
| concentrate-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.156s | 2.156s | 2.156s / 2.621s | 4.574s | 228.5 (30/30) | 114.0 (30/30) | $0.002030 (30/30) |
| concentrate-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 1.943s | 1.943s | 1.943s / 2.576s | 4.602s | 238.0 (30/30) | 115.0 (30/30) | $0.002184 (30/30) |
| direct-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 1.804s | 1.804s | 1.805s / 2.740s | 4.200s | 221.0 (30/30) | 115.0 (25/30) | $0.001902 (30/30) |
| direct-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 1.930s | 1.930s | 1.930s / 2.711s | 4.493s | 236.0 (30/30) | 115.0 (19/30) | $0.002184 (30/30) |
| openrouter-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.023s | 2.023s | 2.043s / 2.518s | 5.136s | 241.0 (30/30) | 0.0 (30/30) | $0.002257 (30/30) |
| openrouter-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 1.937s | 1.937s | 1.948s / 3.099s | 4.973s | 241.0 (30/30) | 0.0 (30/30) | $0.002259 (30/30) |
| vercel-moonshot | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.079s | 2.083s | 2.083s / 3.066s | 4.988s | 242.0 (30/30) | 114.5 (30/30) | $0.002262 (30/30) |
| vercel-moonshot | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 2.035s | 2.045s | 2.045s / 2.410s | 4.670s | 241.0 (30/30) | 114.0 (30/30) | $0.002247 (30/30) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-moonshot | 0.002s | 0.012s | 0.015s |
| concentrate-moonshot | 0.001s | 0.011s | 0.016s |
| direct-moonshot | 0.002s | 0.012s | 0.018s |
| openrouter-moonshot | 0.002s | 0.011s | 0.016s |
| vercel-moonshot | 0.002s | 0.005s | 0.023s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-moonshot | 2.382s | 2.382s | 2.382s | 4.461s |
| concentrate-moonshot | 2.187s | 2.187s | 2.187s | 4.604s |
| direct-moonshot | 1.848s | 1.848s | 1.848s | 4.233s |
| openrouter-moonshot | 2.052s | 2.052s | 2.072s | 5.164s |
| vercel-moonshot | 2.129s | 2.131s | 2.131s | 5.039s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-moonshot | cold | Setup DNS | +0.000s | 30/30 |
| cloudflare-moonshot | cold | Setup TCP | +0.000s | 30/30 |
| cloudflare-moonshot | cold | Setup TLS | -0.003s | 30/30 |
| cloudflare-moonshot | cold | Request to response headers | +0.447s | 30/30 |
| cloudflare-moonshot | cold | Request to first body byte | +0.447s | 30/30 |
| cloudflare-moonshot | cold | Request to semantic TTFT | +0.449s | 30/30 |
| cloudflare-moonshot | cold | Request stream total | +0.605s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end response headers | +0.443s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end first body byte | +0.443s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end semantic TTFT | +0.443s | 30/30 |
| cloudflare-moonshot | cold | Cold end-to-end stream total | +0.598s | 30/30 |
| cloudflare-moonshot | warm | Request to response headers | -0.038s | 30/30 |
| cloudflare-moonshot | warm | Request to first body byte | -0.038s | 30/30 |
| cloudflare-moonshot | warm | Request to semantic TTFT | -0.038s | 30/30 |
| cloudflare-moonshot | warm | Request stream total | -0.405s | 30/30 |
| concentrate-moonshot | cold | Setup DNS | -0.000s | 30/30 |
| concentrate-moonshot | cold | Setup TCP | -0.000s | 30/30 |
| concentrate-moonshot | cold | Setup TLS | -0.002s | 30/30 |
| concentrate-moonshot | cold | Request to response headers | +0.322s | 30/30 |
| concentrate-moonshot | cold | Request to first body byte | +0.322s | 30/30 |
| concentrate-moonshot | cold | Request to semantic TTFT | +0.322s | 30/30 |
| concentrate-moonshot | cold | Request stream total | +0.418s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end response headers | +0.320s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end first body byte | +0.320s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end semantic TTFT | +0.320s | 30/30 |
| concentrate-moonshot | cold | Cold end-to-end stream total | +0.417s | 30/30 |
| concentrate-moonshot | warm | Request to response headers | -0.011s | 30/30 |
| concentrate-moonshot | warm | Request to first body byte | -0.011s | 30/30 |
| concentrate-moonshot | warm | Request to semantic TTFT | -0.011s | 30/30 |
| concentrate-moonshot | warm | Request stream total | -0.050s | 30/30 |
| openrouter-moonshot | cold | Setup DNS | -0.000s | 30/30 |
| openrouter-moonshot | cold | Setup TCP | -0.000s | 30/30 |
| openrouter-moonshot | cold | Setup TLS | -0.003s | 30/30 |
| openrouter-moonshot | cold | Request to response headers | +0.324s | 30/30 |
| openrouter-moonshot | cold | Request to first body byte | +0.324s | 30/30 |
| openrouter-moonshot | cold | Request to semantic TTFT | +0.333s | 30/30 |
| openrouter-moonshot | cold | Request stream total | +1.013s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end response headers | +0.330s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end first body byte | +0.330s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end semantic TTFT | +0.338s | 30/30 |
| openrouter-moonshot | cold | Cold end-to-end stream total | +1.010s | 30/30 |
| openrouter-moonshot | warm | Request to response headers | -0.071s | 30/30 |
| openrouter-moonshot | warm | Request to first body byte | -0.072s | 30/30 |
| openrouter-moonshot | warm | Request to semantic TTFT | -0.067s | 30/30 |
| openrouter-moonshot | warm | Request stream total | +0.405s | 30/30 |
| vercel-moonshot | cold | Setup DNS | +0.000s | 30/30 |
| vercel-moonshot | cold | Setup TCP | -0.006s | 30/30 |
| vercel-moonshot | cold | Setup TLS | +0.004s | 30/30 |
| vercel-moonshot | cold | Request to response headers | +0.332s | 30/30 |
| vercel-moonshot | cold | Request to first body byte | +0.342s | 30/30 |
| vercel-moonshot | cold | Request to semantic TTFT | +0.342s | 30/30 |
| vercel-moonshot | cold | Request stream total | +0.918s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end response headers | +0.325s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end first body byte | +0.336s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end semantic TTFT | +0.335s | 30/30 |
| vercel-moonshot | cold | Cold end-to-end stream total | +0.946s | 30/30 |
| vercel-moonshot | warm | Request to response headers | +0.020s | 30/30 |
| vercel-moonshot | warm | Request to first body byte | +0.033s | 30/30 |
| vercel-moonshot | warm | Request to semantic TTFT | +0.032s | 30/30 |
| vercel-moonshot | warm | Request stream total | -0.171s | 30/30 |
