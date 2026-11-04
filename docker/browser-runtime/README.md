# Browser runtime

Layer the locked browser packages onto an immutable developer runtime. The
source-free image contains no task, reference implementation, or acceptance test.
The runtime is under development; qualification is required before model trials.

The v2 recipe installs the exact official Chrome for Testing full browser and
headless shell recorded in [`fixed-browser-lock.json`](fixed-browser-lock.json),
using the platform-specific download URLs and SHA-256 hashes. The fixed browser
version is 156.0.8078.80; the security floor for CVE-2026-76024 is 156.0.8078.79.
Playwright's original browser manifest stays unchanged. Its cache revision and
declared version describe the bundled browser it expected, rather than the
replacement executable's version. The installer records that distinction, the
downloads and executable version checks in
`/opt/browser-deps/fixed-browser-provenance.json` inside the image.

The lock contains two Playwright cores: the stable library and the CLI's older
alpha. The installer replaces both full/headless cache revisions and provides
the Chrome-channel executable path inside the image. A build control captures
every core's actual full/headless/Chrome spawn targets and rejects a mismatch;
it does not start those browsers. Finished-image qualification must launch those
paths, exercise CLI open/snapshot, replay the current browser worker and a direct
fixture, and renew the sandbox controls before use.

The recipe retains stable Playwright's FFmpeg 1013 installation. The CLI alpha
declares FFmpeg 1011; CLI video recording had no separately provisioned 1011 in
the previous recipe and remains unqualified. This browser repair does not claim
to cover that preexisting video dependency gap.

Build with `scripts/local/build_browser_runtime.py --base-image sha256:…
--receipt <new-receipt.json>`. Its default tag is
`openbench-local/browser-runtime:chrome-156.0.8078.80-v2`; retain older images and
their receipts as historical evidence. The context contains the Dockerfile,
Node package manifests, fixed-browser lock, installer and sandbox gateway. The
receipt hashes every input and includes the source lock as `browser_source_lock`.
That field records build inputs; it does not verify a running browser or admit a
campaign. Rebuild and repeat the browser, sandbox and harness qualification
after changing these inputs. Follow the installation's execution-host policy;
source checks alone do not authorize a build or benchmark run.

The packaged [`browser-seccomp.json`](../../obench/browser-seccomp.json) derives from Microsoft's Apache-2.0 licensed
[Playwright v1.64.0 profile](https://github.com/microsoft/playwright/blob/v1.64.0/utils/docker/seccomp_profile.json).
The upstream [Apache-2.0 license](../../obench/browser-seccomp.LICENSE.md) accompanies the packaged policy.
Its namespace allow rule additionally permits `chroot`: Chromium invokes this
inside its new user namespace; Docker's capability-conditional default blocks it
when the outer container drops all capabilities. No capability is added to the
outer container. Chromium is launched with `chromiumSandbox: true`.

Use this profile only for the browser execution lane. Keep `network=none`,
non-root UID, dropped capabilities, no-new-privileges, a read-only root, bounded
private scratch/shared memory, and no host mounts or published ports. Do not use
host IPC, host networking, SYS_ADMIN, or `--no-sandbox` as launch workarounds.
