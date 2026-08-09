# Set the state-response A/B fixture

Only call tools named `mcp__computer_use__*` to operate
`org.openbench.ComputerUseFixture.v0`. Never call `apply_patch`, shell, browser, filesystem, or
other tools. Do not create artifacts manually; OpenBench collects results
automatically.

Make exactly these five MCP calls in order. Do not make any other MCP calls.

1. Call `get_app_state` once with `app="org.openbench.ComputerUseFixture.v0"` and
   `include_screenshot=false`.
2. In the returned tree, find the line whose label is `toggle-box`. Reuse the
   opaque element ID at the start of that line (for example, `e7@s1`), not the
   label text `toggle-box`, and call `click` with
   `include_state=true` and `include_screenshot=false`.
3. Likewise, use the opaque element ID from the `honest-button` line and call
   `click` twice, as two separate calls, each with
   `include_state=true` and `include_screenshot=false`.
4. Use the opaque element ID from the `keystroke-input` line and call
   `type_text` with `text="openbench-42"`,
   `include_state=true`, and `include_screenshot=false`.

Every element ID must exactly match `e<number>@s<number>` from the returned
tree. It begins with the literal letter `e`; never include indentation,
whitespace, or an extra prefix such as `t`.

Stop after `type_text`. Do not re-read state. The server controls response
encoding; do not request or discuss a response mode. The external checker is
the sole judge of the final fixture state.
