package render

import (
	"strings"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
)

func TestClaudeRenderMemoryFile(t *testing.T) {
	m := &schema.CanonicalMemory{
		Name:        "rg-replace-flag-gotcha",
		Description: "never use rg -r for searching",
		Type:        schema.TypeLesson,
		Scope:       "global",
		Body:        "Body line.\n\n**Why:** reasons.\n",
	}
	r, err := ClaudeRenderer{}.Render(m)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if r.FileName != "rg-replace-flag-gotcha.md" {
		t.Errorf("FileName = %q, want rg-replace-flag-gotcha.md", r.FileName)
	}
	got := string(r.Content)
	for _, want := range []string{
		"name: rg-replace-flag-gotcha",
		"origin: engram-sync",
		"type: lesson",
		"**Why:** reasons.",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("rendered content missing %q:\n%s", want, got)
		}
	}
}

func TestClaudeRenderFrontmatterParses(t *testing.T) {
	m := &schema.CanonicalMemory{
		Name: "x-mem", Description: "d", Type: schema.TypeUser, Scope: "global", Body: "b\n",
	}
	r, err := ClaudeRenderer{}.Render(m)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	var parsed struct {
		Name     string `yaml:"name"`
		Metadata struct {
			Type   string `yaml:"type"`
			Origin string `yaml:"origin"`
		} `yaml:"metadata"`
	}
	if err := yaml.Unmarshal(frontmatter(t, r.Content), &parsed); err != nil {
		t.Fatalf("frontmatter does not parse: %v", err)
	}
	if parsed.Metadata.Origin != marker.Origin {
		t.Errorf("origin = %q, want %q", parsed.Metadata.Origin, marker.Origin)
	}
	if parsed.Metadata.Type != "user" {
		t.Errorf("type = %q, want user", parsed.Metadata.Type)
	}
}

func TestClaudeRenderIndexLineAnchored(t *testing.T) {
	m := &schema.CanonicalMemory{
		Name: "anchor-me", Description: "d", Type: schema.TypeUser, Scope: "global",
	}
	r, err := ClaudeRenderer{}.Render(m)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	name, ok := marker.ClaudeIndexName(r.IndexLine)
	if !ok || name != "anchor-me" {
		t.Errorf("index line not anchored to name: line=%q name=%q ok=%v", r.IndexLine, name, ok)
	}
}

// frontmatter returns the YAML bytes between the opening and closing --- fences.
func frontmatter(t *testing.T, content []byte) []byte {
	t.Helper()
	s := string(content)
	if !strings.HasPrefix(s, "---\n") {
		t.Fatalf("content has no opening frontmatter fence:\n%s", s)
	}
	rest := s[len("---\n"):]
	i := strings.Index(rest, "\n---\n")
	if i < 0 {
		t.Fatalf("content has no closing frontmatter fence:\n%s", s)
	}
	return []byte(rest[:i])
}
