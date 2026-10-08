# Browser runtime

Layer the locked browser packages onto an immutable developer runtime. The
source-free image contains no task, reference implementation, or acceptance test.
The runtime is under development; qualification is required before model trials.

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
