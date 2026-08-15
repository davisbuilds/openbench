# System Settings discovery contract

This local-only task launches a fresh, owned System Settings process and asks
the agent to find the selected wallpaper name and Apple Account personal name.
It must not change a setting.

The verifier hashes the normalized answers and compares them with local oracle
hashes supplied by the benchmark operator. It also requires both plaintext
answers to have appeared in completed `get_app_state` results in the raw Codex
event stream. The retained result artifact contains the wallpaper label and
both hashes, but never the plaintext Apple Account name. Raw events and ATIF
remain private local evidence.

The generated cell seals the resolved hashes into a private immutable oracle.
Its pre-tool hook permits only the exact `get_app_state` and keyboard-search
sequence, blocking clicks and off-contract key or text input before execution.
