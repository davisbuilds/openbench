package render

import (
	"bytes"
	"strings"
	"testing"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
)

func TestCodexRenderCarriesMarkerAndBody(t *testing.T) {
	m := &schema.CanonicalMemory{
		Name: "rg-gotcha", Description: "never use rg -r", Type: schema.TypeLesson,
		Scope: "project:acme-web", Body: "body text here\n",
	}
	r, err := CodexRenderer{}.Render(m)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	name, scope, ok := marker.CodexNoteName(string(r.Content))
	if !ok {
		t.Fatalf("note carries no engram marker:\n%s", r.Content)
	}
	if name != "rg-gotcha" || scope != "project:acme-web" {
		t.Errorf("marker = (%q,%q), want (rg-gotcha, project:acme-web)", name, scope)
	}
	if !strings.Contains(string(r.Content), "body text here") {
		t.Errorf("body not preserved:\n%s", r.Content)
	}
}

func TestCodexRenderIsDeterministic(t *testing.T) {
	m := &schema.CanonicalMemory{Name: "x", Description: "d", Type: schema.TypeUser, Scope: "global"}
	a, _ := CodexRenderer{}.Render(m)
	b, _ := CodexRenderer{}.Render(m)
	if !bytes.Equal(a.Content, b.Content) {
		t.Error("Render must be deterministic (no timestamp in note content)")
	}
}
