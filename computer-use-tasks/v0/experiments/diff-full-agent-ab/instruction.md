# Configure the fixture

Use only tools named `mcp__computer_use__*` to operate
`ComputerUseFixture`. Do not use shell, filesystem, browser, patching, or other
tools, and do not access the fixture's exported state directly.

Finish with this visible application state:

- **Toggle Box** is on.
- **Honest Button** has been activated exactly twice, leaving its counter at 2.
- **Keystroke Input** contains exactly `openbench-42`.

Choose the computer-use tools and arguments yourself. Stop when you believe
the requested state is complete. An external checker judges the fixture-owned
state; tool self-reports and screenshots are not authoritative.
