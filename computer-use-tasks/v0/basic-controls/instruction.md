# Set the basic controls

Only call tools named `mcp__computer_use__*` to operate
`ComputerUseFixture`. Never call `apply_patch`, shell, browser, filesystem, or
other tools. Do not create artifacts manually; OpenBench collects results
automatically.

Inspect state once, then reuse the returned element IDs. On every mutating MCP
call, set `include_state=false` and `include_screenshot=false`.

1. Turn `toggle-box` on.
2. Press `honest-button` exactly twice so the counter is `2`.
3. Enter the exact text `openbench-42` in `keystroke-input`.

Stop after the mutations; do not re-read the full tree. Do not edit the fixture
state export directly. Rely on the external deterministic checker, which judges
the fixture-owned JSON export rather than tool self-reports or screenshots.
