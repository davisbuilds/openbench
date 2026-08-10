# Configure the fixture

Use only tools named `mcp__computer_use__*` to operate
`ComputerUseFixture`. Do not use shell, filesystem, browser, patching, or other
tools, and do not access the fixture's exported state directly.

Finish with this visible application state:

- `toggle-box` is on.
- `honest-button` has been activated exactly twice, leaving its counter at 2.
- `keystroke-input` contains exactly `openbench-42`.

Choose the computer-use tools and arguments yourself. Stop when you believe
the requested state is complete. An external checker judges the fixture-owned
state; tool self-reports and screenshots are not authoritative.

Use each tool's default response behavior; do not set `state_response_mode`.
