package marker

import "testing"

func TestClaudeIndexMarkerRoundTrip(t *testing.T) {
	for _, name := range []string{"rg-replace-flag-gotcha", "a", "x-y-z-2"} {
		line := "- [" + name + "](" + name + ".md) — a hook " + ClaudeIndexMarker(name)
		got, ok := ClaudeIndexName(line)
		if !ok {
			t.Errorf("ClaudeIndexName(%q) reported not-owned; want owned", line)
			continue
		}
		if got != name {
			t.Errorf("ClaudeIndexName(%q) = %q; want %q", line, got, name)
		}
	}
}

func TestClaudeIndexNameRejectsUnmarked(t *testing.T) {
	if got, ok := ClaudeIndexName("- [foo](foo.md) — a hand-authored line"); ok {
		t.Errorf("unmarked line recognized as engram-owned (name=%q)", got)
	}
}

func TestCodexNoteMarkerRoundTrip(t *testing.T) {
	cases := []struct{ name, scope string }{
		{"rg-replace-flag-gotcha", "global"},
		{"local-stack-e2e", "project:example-app"},
	}
	for _, c := range cases {
		note := CodexNoteMarker(c.name, c.scope) + "\n\n# " + c.name + "\nbody\n"
		n, sc, ok := CodexNoteName(note)
		if !ok {
			t.Errorf("CodexNoteName(%q) reported not-owned; want owned", note)
			continue
		}
		if n != c.name || sc != c.scope {
			t.Errorf("CodexNoteName round-trip = (%q,%q); want (%q,%q)", n, sc, c.name, c.scope)
		}
	}
}

func TestCodexNoteNameRejectsForeign(t *testing.T) {
	if _, _, ok := CodexNoteName("<!-- some other extension note -->\n# x\n"); ok {
		t.Error("foreign note recognized as engram-owned")
	}
}

func TestMarkersAreDistinct(t *testing.T) {
	// A Claude index marker must not be misread as a Codex note marker or vice
	// versa; the two harnesses' ownership checks must not cross-match.
	if _, _, ok := CodexNoteName(ClaudeIndexMarker("x")); ok {
		t.Error("Claude index marker matched the Codex note pattern")
	}
	if _, ok := ClaudeIndexName(CodexNoteMarker("x", "global")); ok {
		t.Error("Codex note marker matched the Claude index pattern")
	}
}
