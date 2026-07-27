Gateway Probe
blocks cold=30/30 warm=30/30

| Arm | Condition | Success / availability | Route verified | Request to headers p50 | Request to first body byte p50 | Request to semantic TTFT p50 / p95 | Request stream total p50 | Total tokens p50 (coverage) | Cached input p50 (coverage) | Measured cost p50 (coverage) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cloudflare-openai | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.341s | 0.341s | 0.656s / 1.055s | 0.812s | 51.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| cloudflare-openai | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.260s | 0.260s | 0.561s / 0.739s | 0.754s | 52.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| concentrate-openai | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.404s | 0.404s | 0.763s / 0.946s | 0.930s | 51.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| concentrate-openai | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.361s | 0.361s | 0.704s / 0.933s | 0.896s | 52.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| direct-openai | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.232s | 0.232s | 0.527s / 1.356s | 0.735s | 51.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| direct-openai | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.184s | 0.184s | 0.504s / 0.977s | 0.656s | 52.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| openrouter-openai | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.459s | 0.470s | 0.471s / 0.675s | 0.662s | 54.0 (30/30) | 0.0 (30/30) | $0.000016 (30/30) |
| openrouter-openai | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.423s | 0.432s | 0.433s / 0.655s | 0.587s | 54.5 (30/30) | 0.0 (30/30) | $0.000016 (30/30) |
| vercel-openai | cold | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.773s | 0.783s | 0.789s / 2.494s | 0.951s | 51.0 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |
| vercel-openai | warm | 30/30 (100.0%; 95% CI 88.6%-100.0%) | 30/30 | 0.559s | 0.572s | 0.572s / 1.605s | 0.726s | 52.5 (30/30) | 0.0 (30/30) | $0.000014 (30/30) |

| Arm | Cold setup DNS p50 | TCP p50 | TLS p50 |
|---|---:|---:|---:|
| cloudflare-openai | 0.001s | 0.011s | 0.017s |
| concentrate-openai | 0.001s | 0.011s | 0.018s |
| direct-openai | 0.002s | 0.011s | 0.016s |
| openrouter-openai | 0.001s | 0.011s | 0.016s |
| vercel-openai | 0.002s | 0.006s | 0.024s |

| Arm | Cold end-to-end response headers p50 | First body byte p50 | Semantic TTFT p50 | Stream total p50 |
|---|---:|---:|---:|---:|
| cloudflare-openai | 0.378s | 0.378s | 0.684s | 0.849s |
| concentrate-openai | 0.435s | 0.436s | 0.794s | 0.967s |
| direct-openai | 0.262s | 0.262s | 0.556s | 0.765s |
| openrouter-openai | 0.489s | 0.499s | 0.501s | 0.690s |
| vercel-openai | 0.829s | 0.835s | 0.842s | 1.005s |

| Gateway | Condition | Phase metric | Median gateway - direct | Pair coverage |
|---|---|---|---:|---:|
| cloudflare-openai | cold | Setup DNS | -0.000s | 30/30 |
| cloudflare-openai | cold | Setup TCP | -0.000s | 30/30 |
| cloudflare-openai | cold | Setup TLS | +0.000s | 30/30 |
| cloudflare-openai | cold | Request to response headers | +0.090s | 30/30 |
| cloudflare-openai | cold | Request to first body byte | +0.090s | 30/30 |
| cloudflare-openai | cold | Request to semantic TTFT | +0.095s | 30/30 |
| cloudflare-openai | cold | Request stream total | +0.118s | 30/30 |
| cloudflare-openai | cold | Cold end-to-end response headers | +0.090s | 30/30 |
| cloudflare-openai | cold | Cold end-to-end first body byte | +0.090s | 30/30 |
| cloudflare-openai | cold | Cold end-to-end semantic TTFT | +0.094s | 30/30 |
| cloudflare-openai | cold | Cold end-to-end stream total | +0.115s | 30/30 |
| cloudflare-openai | warm | Request to response headers | +0.081s | 30/30 |
| cloudflare-openai | warm | Request to first body byte | +0.079s | 30/30 |
| cloudflare-openai | warm | Request to semantic TTFT | +0.037s | 30/30 |
| cloudflare-openai | warm | Request stream total | +0.051s | 30/30 |
| concentrate-openai | cold | Setup DNS | -0.000s | 30/30 |
| concentrate-openai | cold | Setup TCP | +0.000s | 30/30 |
| concentrate-openai | cold | Setup TLS | +0.001s | 30/30 |
| concentrate-openai | cold | Request to response headers | +0.169s | 30/30 |
| concentrate-openai | cold | Request to first body byte | +0.169s | 30/30 |
| concentrate-openai | cold | Request to semantic TTFT | +0.193s | 30/30 |
| concentrate-openai | cold | Request stream total | +0.199s | 30/30 |
| concentrate-openai | cold | Cold end-to-end response headers | +0.167s | 30/30 |
| concentrate-openai | cold | Cold end-to-end first body byte | +0.167s | 30/30 |
| concentrate-openai | cold | Cold end-to-end semantic TTFT | +0.191s | 30/30 |
| concentrate-openai | cold | Cold end-to-end stream total | +0.198s | 30/30 |
| concentrate-openai | warm | Request to response headers | +0.168s | 30/30 |
| concentrate-openai | warm | Request to first body byte | +0.168s | 30/30 |
| concentrate-openai | warm | Request to semantic TTFT | +0.202s | 30/30 |
| concentrate-openai | warm | Request stream total | +0.201s | 30/30 |
| openrouter-openai | cold | Setup DNS | -0.000s | 30/30 |
| openrouter-openai | cold | Setup TCP | -0.000s | 30/30 |
| openrouter-openai | cold | Setup TLS | -0.001s | 30/30 |
| openrouter-openai | cold | Request to response headers | +0.233s | 30/30 |
| openrouter-openai | cold | Request to first body byte | +0.243s | 30/30 |
| openrouter-openai | cold | Request to semantic TTFT | -0.088s | 30/30 |
| openrouter-openai | cold | Request stream total | -0.095s | 30/30 |
| openrouter-openai | cold | Cold end-to-end response headers | +0.230s | 30/30 |
| openrouter-openai | cold | Cold end-to-end first body byte | +0.242s | 30/30 |
| openrouter-openai | cold | Cold end-to-end semantic TTFT | -0.088s | 30/30 |
| openrouter-openai | cold | Cold end-to-end stream total | -0.097s | 30/30 |
| openrouter-openai | warm | Request to response headers | +0.230s | 30/30 |
| openrouter-openai | warm | Request to first body byte | +0.250s | 30/30 |
| openrouter-openai | warm | Request to semantic TTFT | -0.054s | 30/30 |
| openrouter-openai | warm | Request stream total | -0.056s | 30/30 |
| vercel-openai | cold | Setup DNS | +0.000s | 30/30 |
| vercel-openai | cold | Setup TCP | -0.005s | 30/30 |
| vercel-openai | cold | Setup TLS | +0.008s | 30/30 |
| vercel-openai | cold | Request to response headers | +0.508s | 30/30 |
| vercel-openai | cold | Request to first body byte | +0.516s | 30/30 |
| vercel-openai | cold | Request to semantic TTFT | +0.227s | 30/30 |
| vercel-openai | cold | Request stream total | +0.235s | 30/30 |
| vercel-openai | cold | Cold end-to-end response headers | +0.511s | 30/30 |
| vercel-openai | cold | Cold end-to-end first body byte | +0.519s | 30/30 |
| vercel-openai | cold | Cold end-to-end semantic TTFT | +0.229s | 30/30 |
| vercel-openai | cold | Cold end-to-end stream total | +0.238s | 30/30 |
| vercel-openai | warm | Request to response headers | +0.348s | 30/30 |
| vercel-openai | warm | Request to first body byte | +0.363s | 30/30 |
| vercel-openai | warm | Request to semantic TTFT | +0.058s | 30/30 |
| vercel-openai | warm | Request stream total | +0.071s | 30/30 |
