# Post-action state A/B contract

This task compares post-action state recapture enabled (`state`) and disabled
(`no-state`) while holding the source server, initial state read, mutations,
screenshots, and external checker constant. The `state` arm locks automatic
state-response encoding; `no-state` must omit a response mode because the MCP
contract rejects `state_response_mode` when `include_state=false`. The sealed
call ledger must contain exactly the five arm-specific calls or the native trial
is non-comparable. The fixture-owned JSON export is the sole success authority.
