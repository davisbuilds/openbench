# Preserve canonical memories during batch curation

This workspace contains Engram, a Go CLI that stores Markdown memories and
renders them for coding agents. Curation accepts a batch of proposed memory
operations and applies them to the canonical store. Fix two reported behaviors.

## 1. Valid batches lose earlier changes

Some successful batches produce a final memory with its requested scope but old
content. Other valid batches fail after already writing some of their changes.
The final state of a valid batch must reflect every operation in order, including
content, descriptive metadata, and scope. Adding or merging a memory and then
operating on its resulting identity must work as well as operating on an existing
memory. Unrelated memories must remain byte-for-byte intact.

## 2. Incomplete canonical stores are treated as safe to edit

Curation can report success when one of the existing canonical Markdown files
cannot be parsed. It may overwrite that unreadable file or modify other memories
while the store is in this state. If any canonical memory cannot be parsed, reject
the whole batch before changing any canonical memory's bytes or file membership.
The identical valid proposal must succeed once that malformed input is removed.

## Preserve the existing contract

Valid add, update, remove, merge and scope-change operations must keep working.
Reject invalid proposals as a whole, including collisions between operations and
merges that would overwrite an unrelated memory. Respect the canonical mutation
lock and validate against the current store. Keep the existing command and data
interfaces compatible. No network service or live agent is needed for this fix.

Change implementation files as needed; you may add your own local tests. Hidden
checks award equal credit for the two behaviors, provided existing regression
controls still pass. Go modules are pinned in go.mod and go.sum; the environment
must provide Go and the cached dependencies before offline testing.
