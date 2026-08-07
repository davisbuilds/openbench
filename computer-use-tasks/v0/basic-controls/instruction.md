# Set the basic controls

Only call tools named `mcp__computer_use__*` to operate
`ComputerUseFixture`. Never call `apply_patch`, shell, browser, filesystem, or
other tools. Do not create artifacts manually; OpenBench collects results
automatically.

1. Turn `toggle-box` on.
2. Press `honest-button` exactly twice so the counter is `2`.
3. Enter the exact text `openbench-42` in `keystroke-input`.

Do not edit the fixture state export directly. Completion is judged from the
fixture-owned JSON export, not from tool self-reports or screenshots.
