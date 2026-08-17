# Set the basic controls

Use the installed Codex Computer Use capability to operate only the already
running, frontmost app with bundle identifier
`org.openbench.ComputerUseFixture.v0`. Use that bundle identifier as the `app`
selector; do not launch or select another copy by application name. Do not use
shell, filesystem, browser, or code-editing tools to complete the task.

1. Turn `toggle-box` on.
2. Press `honest-button` exactly twice so the counter is `2`.
3. Enter the exact text `openbench-42` in `keystroke-input`.

Stop after the requested state is reached. Do not edit the fixture state export
directly. An external deterministic checker judges the fixture-owned JSON
export rather than the agent's final answer.
