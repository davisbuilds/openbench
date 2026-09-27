# Dojo evidence repair v5

This task retains the v4 instruction and buggy Python source. Only the versioned
budget oracle changes: an unsupported mode must return an unsupported verdict and
must not gate builds; diagnostic entry counts may be retained. Public field types
remain required. See [provenance](PROVENANCE.md).

Execution uses the same isolated repair lane, scheme-3 task binding, and trusted
external grader as v4. Fresh model trials are a new treatment. Historical results
must be replayed with their pinned source and must not be relabeled or pooled.
