# AgentMonitor PR106 v4 provenance

Source and solver instruction are byte-identical to v3. Oracle v3 queries preserved
benchmark rows by their source rather than requiring the pre-migration event ID.
The instruction explicitly permits storage event/session ID choices. The same
row count, study fields, tokens, supported new event type, and repeat startup
checks remain required. Historical oracle v2 and sealed results are unchanged.
Original source provenance: [v3 provenance](../am-benchmark-pr106-v3/PROVENANCE.md).
