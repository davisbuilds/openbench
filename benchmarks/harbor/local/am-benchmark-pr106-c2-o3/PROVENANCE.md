# AgentMonitor backend development case 2 / oracle 3

The source and grading contract match historical `am-benchmark-pr106-v4`.
This revision adds development instructions and the public
`tests/benchmark-usage-segregation.test.ts` from pre-review commit
`4cfbddaa788dddb205d19225135cab44578f5fbe` of
https://github.com/davisbuilds/agentmonitor. The original test is copied verbatim;
its revision and checksum are in SOURCE_MANIFEST.json. It tests existing usage
and HTTP behavior, without importing historical reference repairs or the hidden
oracle. Other old tests that assert implementation-specific storage identifiers
are deliberately not included. Package metadata and source stay unchanged.

Case revision advances for the additional model-visible tests/instructions.
Oracle revision remains 3. Use the developer runtime and renew admission before
fresh trials. Historical packages/results retain their original identities.
Source construction and licensing: [original provenance](../../../local/am-benchmark-pr106-v2/PROVENANCE.md).
