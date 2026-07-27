Gateway Probe
blocks cold=1/1 warm=1/1

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-moonshot | cold | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.028s | 2.028s | 2.029s / 2.029s | 5.992s | 246.0 (1/1) | 118.0 (1/1) | $0.002274 (1/1) |
| cloudflare-moonshot | warm | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.878s | 2.878s | 2.879s / 2.879s | 6.236s | 244.0 (1/1) | 116.0 (1/1) | $0.002268 (1/1) |
| concentrate-moonshot | cold | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.204s | 2.204s | 2.204s / 2.204s | 6.562s | 246.0 (1/1) | 118.0 (1/1) | $0.002274 (1/1) |
| concentrate-moonshot | warm | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.360s | 2.360s | 2.360s / 2.360s | 5.599s | 244.0 (1/1) | 116.0 (1/1) | $0.002268 (1/1) |
| direct-moonshot | cold | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 1.955s | 1.955s | 1.955s / 1.955s | 4.818s | 232.0 (1/1) | 118.0 (1/1) | $0.002064 (1/1) |
| direct-moonshot | warm | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.294s | 2.294s | 2.294s / 2.294s | 3.405s | 165.0 (1/1) | 116.0 (1/1) | $0.001083 (1/1) |
| openrouter-moonshot | cold | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.314s | 2.315s | 2.324s / 2.324s | 4.392s | 199.0 (1/1) | 0.0 (1/1) | $0.001569 (1/1) |
| openrouter-moonshot | warm | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 1.852s | 1.852s | 1.856s / 1.856s | 4.504s | 213.0 (1/1) | 116.0 (1/1) | $0.001803 (1/1) |
| vercel-moonshot | cold | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.837s | 2.837s | 2.837s / 2.837s | 6.377s | 246.0 (1/1) | 118.0 (1/1) | $0.002274 (1/1) |
| vercel-moonshot | warm | 1/1 (100.0%; 95% CI 20.7%-100.0%) | 1/1 | 2.541s | 2.546s | 2.546s / 2.546s | 6.162s | 244.0 (1/1) | 116.0 (1/1) | $0.002268 (1/1) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-moonshot | 0.001s | 0.011s | 0.018s |
| concentrate-moonshot | 0.001s | 0.011s | 0.020s |
| direct-moonshot | 0.023s | 0.011s | 0.017s |
| openrouter-moonshot | 0.001s | 0.012s | 0.016s |
| vercel-moonshot | 0.002s | 0.005s | 0.021s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-moonshot | 2.059s | 2.059s | 2.060s | 6.023s |
| concentrate-moonshot | 2.237s | 2.237s | 2.237s | 6.595s |
| direct-moonshot | 2.006s | 2.006s | 2.006s | 4.869s |
| openrouter-moonshot | 2.343s | 2.344s | 2.353s | 4.421s |
| vercel-moonshot | 2.865s | 2.865s | 2.865s | 6.406s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-moonshot | cold | Setup DNS | -0.021s | 1/1 |
| cloudflare-moonshot | cold | Setup TCP | -0.000s | 1/1 |
| cloudflare-moonshot | cold | Setup TLS | +0.001s | 1/1 |
| cloudflare-moonshot | cold | Request to response headers | +0.073s | 1/1 |
| cloudflare-moonshot | cold | Request to first body byte | +0.073s | 1/1 |
| cloudflare-moonshot | cold | Request to semantic TTFT | +0.074s | 1/1 |
| cloudflare-moonshot | cold | Request stream total | +1.175s | 1/1 |
| cloudflare-moonshot | cold | Cold end-to-end response headers | +0.053s | 1/1 |
| cloudflare-moonshot | cold | Cold end-to-end first body byte | +0.053s | 1/1 |
| cloudflare-moonshot | cold | Cold end-to-end semantic TTFT | +0.054s | 1/1 |
| cloudflare-moonshot | cold | Cold end-to-end stream total | +1.154s | 1/1 |
| cloudflare-moonshot | warm | Request to response headers | +0.584s | 1/1 |
| cloudflare-moonshot | warm | Request to first body byte | +0.584s | 1/1 |
| cloudflare-moonshot | warm | Request to semantic TTFT | +0.585s | 1/1 |
| cloudflare-moonshot | warm | Request stream total | +2.832s | 1/1 |
| concentrate-moonshot | cold | Setup DNS | -0.021s | 1/1 |
| concentrate-moonshot | cold | Setup TCP | +0.000s | 1/1 |
| concentrate-moonshot | cold | Setup TLS | +0.003s | 1/1 |
| concentrate-moonshot | cold | Request to response headers | +0.250s | 1/1 |
| concentrate-moonshot | cold | Request to first body byte | +0.250s | 1/1 |
| concentrate-moonshot | cold | Request to semantic TTFT | +0.250s | 1/1 |
| concentrate-moonshot | cold | Request stream total | +1.745s | 1/1 |
| concentrate-moonshot | cold | Cold end-to-end response headers | +0.231s | 1/1 |
| concentrate-moonshot | cold | Cold end-to-end first body byte | +0.231s | 1/1 |
| concentrate-moonshot | cold | Cold end-to-end semantic TTFT | +0.231s | 1/1 |
| concentrate-moonshot | cold | Cold end-to-end stream total | +1.726s | 1/1 |
| concentrate-moonshot | warm | Request to response headers | +0.066s | 1/1 |
| concentrate-moonshot | warm | Request to first body byte | +0.066s | 1/1 |
| concentrate-moonshot | warm | Request to semantic TTFT | +0.066s | 1/1 |
| concentrate-moonshot | warm | Request stream total | +2.194s | 1/1 |
| openrouter-moonshot | cold | Setup DNS | -0.021s | 1/1 |
| openrouter-moonshot | cold | Setup TCP | +0.001s | 1/1 |
| openrouter-moonshot | cold | Setup TLS | -0.002s | 1/1 |
| openrouter-moonshot | cold | Request to response headers | +0.360s | 1/1 |
| openrouter-moonshot | cold | Request to first body byte | +0.360s | 1/1 |
| openrouter-moonshot | cold | Request to semantic TTFT | +0.369s | 1/1 |
| openrouter-moonshot | cold | Request stream total | -0.426s | 1/1 |
| openrouter-moonshot | cold | Cold end-to-end response headers | +0.337s | 1/1 |
| openrouter-moonshot | cold | Cold end-to-end first body byte | +0.337s | 1/1 |
| openrouter-moonshot | cold | Cold end-to-end semantic TTFT | +0.347s | 1/1 |
| openrouter-moonshot | cold | Cold end-to-end stream total | -0.448s | 1/1 |
| openrouter-moonshot | warm | Request to response headers | -0.442s | 1/1 |
| openrouter-moonshot | warm | Request to first body byte | -0.442s | 1/1 |
| openrouter-moonshot | warm | Request to semantic TTFT | -0.438s | 1/1 |
| openrouter-moonshot | warm | Request stream total | +1.099s | 1/1 |
| vercel-moonshot | cold | Setup DNS | -0.020s | 1/1 |
| vercel-moonshot | cold | Setup TCP | -0.006s | 1/1 |
| vercel-moonshot | cold | Setup TLS | +0.004s | 1/1 |
| vercel-moonshot | cold | Request to response headers | +0.882s | 1/1 |
| vercel-moonshot | cold | Request to first body byte | +0.882s | 1/1 |
| vercel-moonshot | cold | Request to semantic TTFT | +0.882s | 1/1 |
| vercel-moonshot | cold | Request stream total | +1.560s | 1/1 |
| vercel-moonshot | cold | Cold end-to-end response headers | +0.859s | 1/1 |
| vercel-moonshot | cold | Cold end-to-end first body byte | +0.859s | 1/1 |
| vercel-moonshot | cold | Cold end-to-end semantic TTFT | +0.859s | 1/1 |
| vercel-moonshot | cold | Cold end-to-end stream total | +1.537s | 1/1 |
| vercel-moonshot | warm | Request to response headers | +0.246s | 1/1 |
| vercel-moonshot | warm | Request to first body byte | +0.251s | 1/1 |
| vercel-moonshot | warm | Request to semantic TTFT | +0.251s | 1/1 |
| vercel-moonshot | warm | Request stream total | +2.757s | 1/1 |
