package schema

import (
	"reflect"
	"strings"
	"testing"
)

func TestParseRenderRoundTrip(t *testing.T) {
	cases := []struct {
		name string
		mem  CanonicalMemory
	}{
		{
			name: "full memory",
			mem: CanonicalMemory{
				Name:        "rg-replace-flag-gotcha",
				Description: "never use rg -r for searching; it replaces matches",
				Type:        TypeLesson,
				Scope:       "global",
				AppliesTo: AppliesTo{
					Cwd:    []string{"/**"},
					Agents: []string{"claude", "codex"},
					Hosts:  []string{"host-a"},
				},
				Provenance: Provenance{
					Origin:   "engram-sync",
					Author:   "someone",
					Created:  "2026-08-26T00:00:00Z",
					Modified: "2026-08-26T00:00:00Z",
				},
				Related: []string{"zsh-no-word-splitting"},
				Body:    "Body line one.\n\n**Why:** because.\n**How to apply:** carefully.\n",
			},
		},
		{
			name: "minimal required-only, empty body",
			mem: CanonicalMemory{
				Name:        "local-stack-e2e",
				Description: "playwright against a local dev stack",
				Type:        TypeProject,
				Scope:       "project:example-app",
			},
		},
	}
	for _, c := range cases {
		out, err := c.mem.Render()
		if err != nil {
			t.Fatalf("%s: Render: %v", c.name, err)
		}
		got, err := Parse(out)
		if err != nil {
			t.Fatalf("%s: Parse: %v", c.name, err)
		}
		if !reflect.DeepEqual(*got, c.mem) {
			t.Errorf("%s: round-trip mismatch\n got: %#v\nwant: %#v", c.name, *got, c.mem)
		}
	}
}

func TestParseMissingFrontmatter(t *testing.T) {
	if _, err := Parse([]byte("no frontmatter here\njust body\n")); err == nil {
		t.Error("expected error parsing content without frontmatter, got nil")
	}
}

func TestParseUnterminatedFrontmatter(t *testing.T) {
	if _, err := Parse([]byte("---\nname: x\n")); err == nil {
		t.Error("expected error for unterminated frontmatter, got nil")
	}
}

func TestParseRejectsMergeMarkers(t *testing.T) {
	// A git-conflicted canonical file must be refused, never parsed and rendered.
	conflicted := "---\nname: x\ndescription: y\ntype: lesson\nscope: global\n---\n" +
		"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
	if _, err := Parse([]byte(conflicted)); err == nil {
		t.Error("expected error for content with merge-conflict markers, got nil")
	}
}

func TestValidateRequiredFields(t *testing.T) {
	err := (&CanonicalMemory{}).Validate()
	if err == nil {
		t.Fatal("expected validation error for empty memory, got nil")
	}
	for _, want := range []string{"name", "description", "type", "scope"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("validation error should mention missing %q; got: %v", want, err)
		}
	}
}

func TestValidateEnumScopeAndName(t *testing.T) {
	bad := []struct {
		name string
		mem  CanonicalMemory
	}{
		{"bad type", CanonicalMemory{Name: "a", Description: "d", Type: "nonsense", Scope: "global"}},
		{"bad scope", CanonicalMemory{Name: "a", Description: "d", Type: TypeUser, Scope: "team:x"}},
		{"non-kebab name", CanonicalMemory{Name: "Not Kebab", Description: "d", Type: TypeUser, Scope: "global"}},
	}
	for _, c := range bad {
		if err := c.mem.Validate(); err == nil {
			t.Errorf("%s: expected validation error, got nil", c.name)
		}
	}

	good := []CanonicalMemory{
		{Name: "ok-name", Description: "d", Type: TypeUser, Scope: "global"},
		{Name: "ok-name-2", Description: "d", Type: TypeProject, Scope: "project:acme-web"},
	}
	for _, m := range good {
		if err := m.Validate(); err != nil {
			t.Errorf("expected valid memory %q, got error: %v", m.Name, err)
		}
	}
}
