# Set the state-response A/B fixture

Only call tools named `mcp__computer_use__*` to operate
`ComputerUseFixture`. Never call `apply_patch`, shell, browser, filesystem, or
other tools. Do not create artifacts manually; OpenBench collects results
automatically.

Make exactly these five MCP calls in order. Do not make any other MCP calls.

1. Call `get_app_state` once with `app="ComputerUseFixture"` and
   `include_screenshot=false`.
2. Reuse returned element IDs and call `click` on `toggle-box` with
   `include_state=true` and `include_screenshot=false`.
3. Call `click` on `honest-button` twice, as two separate calls, each with
   `include_state=true` and `include_screenshot=false`.
4. Call `type_text` on `keystroke-input` with `text="openbench-42"`,
   `include_state=true`, and `include_screenshot=false`.

Stop after `type_text`. Do not re-read state. The server controls response
encoding; do not request or discuss a response mode. The external checker is
the sole judge of the final fixture state.
