# Set the state-response A/B fixture

Only call tools named `mcp__computer_use__*` to operate
`org.openbench.ComputerUseFixture.v0`. Never call `apply_patch`, shell, browser, filesystem, or
other tools. Do not create artifacts manually; OpenBench collects results
automatically.

Make exactly these five MCP calls in order. Do not make any other MCP calls.

1. Call `get_app_state` once with `app="org.openbench.ComputerUseFixture.v0"` and
   `include_screenshot=false`.
2. Confirm the returned tree contains `e7@s1 AXCheckBox "toggle-box"`, then
   call `click` on element ID `e7@s1` with
   `include_state=true` and `include_screenshot=false`.
3. Confirm the tree contains `e6@s1 AXButton "honest-button"`, then call
   `click` on element ID `e6@s1` twice, as two separate calls, each with
   `include_state=true` and `include_screenshot=false`.
4. Confirm the tree contains `e11@s1 AXTextArea "keystroke-input"`, then call
   `type_text` on element ID `e11@s1` with `text="openbench-42"`,
   `include_state=true`, and `include_screenshot=false`.

Stop after `type_text`. Do not re-read state. The server controls response
encoding; do not request or discuss a response mode. The external checker is
the sole judge of the final fixture state.
