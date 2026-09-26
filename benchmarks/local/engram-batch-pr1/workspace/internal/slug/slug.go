// Package slug encodes a working directory into the project-directory slug that
// Claude Code uses to key its per-project memory store.
package slug

import "strings"

// ForCwd returns the Claude Code project slug for an absolute working directory.
// Claude Code derives the slug by replacing every character that is not an ASCII
// letter or digit with '-', so "/Users/alice/Dev" becomes "-Users-alice-Dev".
// The transform is lossy (many characters collapse to '-') and therefore not
// invertible; it is deterministic and idempotent.
func ForCwd(cwd string) string {
	return strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			return r
		default:
			return '-'
		}
	}, cwd)
}
