# Control the inactive fixture

Keep the provided guard application foreground for the entire agent phase.
Without activating `BackgroundControlFixture` and without escalating to global
input delivery:

1. Set `backgroundSafeTextField` to the exact text `openbench-background-42`.
2. Press `pressableButton` so its status is `pressed`.
3. Select `Fixture Menu Item` so the menu status is `menu`.

Only call tools named `mcp__computer_use__*`. Never call `apply_patch`, shell,
browser, filesystem, or other tools. Do not create artifacts manually or edit
fixture state or runner-owned focus evidence. OpenBench collects results
automatically. Completion requires exact fixture state plus the native
runner's sealed focus and delivery ledger.
