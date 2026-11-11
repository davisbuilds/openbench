# Browser runtime

Layer the locked browser packages onto an immutable developer runtime. The
source-free image contains no task, reference implementation, or acceptance test.
The runtime is under development; qualification is required before model trials.

The v3 recipe installs the Chromium full browser, headless shell and FFmpeg
selected by each exact locked Playwright core through its upstream installer.
Playwright 1.65.0 selects Chromium 158.0.8127.18 (revision 1254); CLI 0.1.23's
core 1.65.0-alpha-2026-10-30 selects Chromium 157.0.8103.15 (revision 1252).
Current preflights, interactive review and receipt import accept exactly the
qualified 1.65.0 library and historical 1.64.0 evidence; image identity and
actual browser checks remain separate. Both upstream browser versions clear the CVE-2026-76024 floor of 156.0.8078.79. No external Chrome pin,
archive replacement or browser-manifest rewrite is used. The Chrome channel
used by the default CLI points to the stable library's upstream full browser.

Full Chrome retains a launcher that gives only the browser process a private
configuration directory under `/tmp`. Its crash handler needs this when the
worker's home and root filesystem are read-only. Agent and authentication
configuration retain their existing XDG environment. The retained vendor bytes,
launcher hashes, original manifests and executable version checks are recorded
in `/opt/browser-deps/browser-provenance.json` inside the image.

Run `scripts/local/verify_browser_runtime.py --image sha256:… --output
<new-receipt.json>` for default headless, full and Chrome-channel interactions
through both cores, plus the default CLI's open/click/snapshot path. The helper
uses the existing non-root, offline, read-only browser worker boundary and
checks each core's actual browser version and absence of `--no-sandbox`. The
browser CI lane runs this same control. Finished-image qualification also
replays the current browser worker and direct fixtures and renews sandbox
controls before use. An install/build receipt alone does not admit a campaign.

Build with `scripts/local/build_browser_runtime.py --base-image sha256:…
--receipt <new-receipt.json>`. Its default tag is
`openbench-local/browser-runtime:playwright-1.65.0-v3`; retain older images and
their receipts as historical evidence. The context contains the Dockerfile,
Node package manifests, runtime installer and sandbox gateway. The receipt
hashes every input. Rebuild and repeat browser, sandbox and harness
qualification after changing these inputs. Follow the installation's
execution-host policy; source checks alone do not authorize a benchmark run.

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
