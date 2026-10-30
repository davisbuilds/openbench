# AgentMonitor backend development case 3 / oracle 3

The source and grading contract match historical `am-benchmark-pr106-v4`.
This revision adds development instructions and the public
`tests/benchmark-usage-segregation.test.ts` from pre-review commit
`4cfbddaa788dddb205d19225135cab44578f5fbe` of
https://github.com/davisbuilds/agentmonitor. The original test is copied verbatim;
its revision and checksum are in SOURCE_MANIFEST.json. It tests existing usage
and HTTP behavior, without importing historical reference repairs or the hidden
oracle. Other old tests that assert implementation-specific storage identifiers
are deliberately not included. Package dependency metadata and source stay unchanged. The package scripts
are narrowed to the supported backend test/watch/typecheck workflows; the
source manifest records that derivation.

Case 2 advanced for the additional model-visible tests/instructions.
Oracle revision remains 3. Use the developer runtime and renew admission before
fresh trials. Historical packages/results retain their original identities.
Source construction and licensing: [original provenance](../../../local/am-benchmark-pr106-v2/PROVENANCE.md).

## Patched-runtime execution revision

Case 3 derives from the retained `am-benchmark-pr106-c2-o3` package. Its only
model-visible change is `environment/app/package.json` field `engines.pnpm`,
from `10.29.3` to `10.34.7`, matching the patched runtime. The original manifest,
SHA-256 hashes and exact diff are retained under `execution-revision/`; the
source manifest records the revised checksum. `packageManager`, Node engines,
lockfile, dependencies, source, instructions and public tests are unchanged.
The runtime invokes its pinned pnpm directly with package-manager version
management disabled; it does not download the historical packageManager pin.

This is a bounded workflow-control adaptation, not a new performance result.
Case revision advances because the supplied manifest changed; oracle revision
stays 3. Both comparison sides and OFF/ON treatments must use the same revised
export if this package is used in a comparison. Historical packages and results
remain unchanged. Renew runtime admission before executing the revised control.
The generic runtime image contains no task fixture or developer-control script,
so this revision alone does not require rebuilding that image.
