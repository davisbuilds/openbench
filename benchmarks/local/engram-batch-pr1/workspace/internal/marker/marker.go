// Package marker defines engram's provenance markers — the identity contract by
// which engram recognizes its own output in a harness memory store. Every file
// or line engram writes carries a marker; anything without one is hand-authored
// and off-limits. Stale detection, import loop-guards, and conflict handling all
// read these markers, so their formats change only in lockstep across the
// renderer, sync, importer, and tests.
package marker

import (
	"fmt"
	"regexp"
)

var (
	claudeIndexRe = regexp.MustCompile(`<!-- engram name=([a-z0-9-]+) -->`)
	codexNoteRe   = regexp.MustCompile(`<!-- engram-sync canonical=([a-z0-9-]+) scope=(\S+) extension=engram -->`)
)

// Origin is the provenance value engram writes into a Claude Code memory file's
// metadata.origin. A memory file whose origin differs, or is absent, is
// hand-authored and must not be modified or deleted by engram.
const Origin = "engram-sync"

// Extension is the Codex external-adapter extension name engram writes under
// (~/.codex/memories/extensions/<Extension>/).
const Extension = "engram"

// ClaudeIndexMarker returns the HTML-comment marker embedded in a Claude Code
// MEMORY.md index line. The embedded name is the stable anchor by which the line
// is located for update, rename, or removal — no update can duplicate or clobber
// the wrong line.
func ClaudeIndexMarker(name string) string {
	return fmt.Sprintf("<!-- engram name=%s -->", name)
}

// ClaudeIndexName extracts the memory name from a MEMORY.md line and reports
// whether the line is engram-owned at all.
func ClaudeIndexName(line string) (string, bool) {
	m := claudeIndexRe.FindStringSubmatch(line)
	if m == nil {
		return "", false
	}
	return m[1], true
}

// CodexNoteMarker returns the marker prefixing a Codex extension note. It carries
// both the canonical name and the scope so the note is self-identifying.
func CodexNoteMarker(name, scope string) string {
	return fmt.Sprintf("<!-- engram-sync canonical=%s scope=%s extension=%s -->", name, scope, Extension)
}

// CodexNoteName extracts the canonical name and scope from a Codex note's marker
// and reports whether the content is engram-owned.
func CodexNoteName(s string) (name, scope string, ok bool) {
	m := codexNoteRe.FindStringSubmatch(s)
	if m == nil {
		return "", "", false
	}
	return m[1], m[2], true
}
