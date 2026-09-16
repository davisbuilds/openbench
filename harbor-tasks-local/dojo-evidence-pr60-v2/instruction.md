The profile evidence tools sometimes report a reassuring result that disagrees with the session being inspected.

Please fix these behaviors while preserving the existing public functions and return shapes:

- Conversation examples and summaries can displace the actual skills listing when reading a recorded session. Sessions with no actual listing should remain distinguishable from sessions with an empty listing.
- A budget result from a different invocation mode can be treated as deployable and allowed to fail a build. Policies should apply only to the modes they declare; observations without a declared mode need the same care.
- Comparing two listings can miss a removed duplicate or a replacement from a different source, even though the visible names look the same. Equivalent listings should still compare equal, including when locator aliases or plugin version paths differ.

Make the behavior consistent across the profile evidence tools. Do not call a live harness or model. The repository slice uses Python and PyYAML; no other runtime dependency is needed.
