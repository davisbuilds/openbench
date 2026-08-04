# Add `@include` support to miniconf

`miniconf` is a small INI-style configuration parser (see `miniconf/`). It
supports comments, `[sections]`, `key = value` assignments, and typed values.
Its existing test suite is under `tests/` and passes.

We need configuration files to be able to pull in other files. Add support for
an `@include` directive to the file loader.

## Directive syntax

A line whose first whitespace-separated token is `@include`, followed by a
path, includes another configuration file at that point:

```
@include logging.conf
```

## Required semantics

- **Inline expansion.** An `@include` line is replaced by the full contents of
  the referenced file, in place, as if those lines had been written where the
  directive appears. Parsing (sections, keys, comments, value coercion) then
  proceeds over the expanded text exactly as today.
- **Relative paths.** The include path is resolved relative to the directory of
  the file that contains the directive (not the process working directory).
- **Ordering / override.** Because expansion is positional, a key assigned
  *after* an `@include` overrides a value that the included file set for the
  same key, and a key assigned before it is overridden by the include. This
  follows from normal "later assignment wins" behavior once the file is
  expanded.
- **Nesting.** An included file may itself contain `@include` directives; expand
  them recursively.
- **Missing file.** If an include target does not exist, raise
  `miniconf.errors.ConfigError`.
- **Cycles.** If a file includes itself directly or transitively (a cycle),
  raise `miniconf.errors.ConfigError` rather than looping forever.

Only `miniconf.loader.load(path)` needs to handle includes. The behavior of
every existing function, and of files that contain no `@include` directives,
must not change — the current test suite must still pass.

An example pair of files is provided in `examples/` (`service.conf` includes
`logging.conf`) to try your implementation against.

## Done when

`miniconf.loader.load(path)` expands `@include` directives per the semantics
above, existing behavior is unchanged, and the existing test suite still
passes.
