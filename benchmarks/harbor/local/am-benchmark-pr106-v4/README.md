# AgentMonitor PR106 v4

Versioned correction to the migration observer: preserving event data does not
require retaining its internal event ID. Uses registered oracle
`agentmonitor-benchmark-v3`, the same confined worker protocol, and scheme-4
binding. Source and instructions match v3. Replay old results at their pinned
commit; new observations are a separate treatment.
