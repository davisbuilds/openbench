package review

import (
	"testing"

	"github.com/davisbuilds/engram/internal/schema"
)

func mem(name, scope string, cwd ...string) *schema.CanonicalMemory {
	return &schema.CanonicalMemory{
		Name: name, Description: "d", Type: schema.TypeLesson, Scope: scope,
		AppliesTo: schema.AppliesTo{Cwd: cwd},
	}
}

func findingsOfKind(fs []Finding, kind string) []Finding {
	var out []Finding
	for _, f := range fs {
		if f.Kind == kind {
			out = append(out, f)
		}
	}
	return out
}

func TestAnalyzeFlagsNearDuplicateNames(t *testing.T) {
	mems := []*schema.CanonicalMemory{
		mem("rg-replace-flag-gotcha", "global"),
		mem("rg-replace-flag", "global"),
		mem("totally-different-thing", "global"),
	}
	dupes := findingsOfKind(Analyze(mems), "near-duplicate")
	if len(dupes) != 1 {
		t.Fatalf("expected 1 near-duplicate finding, got %d: %+v", len(dupes), dupes)
	}
	if len(dupes[0].Names) != 2 {
		t.Errorf("near-duplicate should name both memories: %+v", dupes[0])
	}
}

// A project-scoped memory with no cwd glob is the normal, correct shape (tier
// matching uses the scope's path segment, not the cwd axis), so review must NOT
// flag it — scope judgment belongs to curate, not a static heuristic.
func TestAnalyzeDoesNotFlagProjectScopeAsFinding(t *testing.T) {
	mems := []*schema.CanonicalMemory{
		mem("alpha-widget", "project:acme"),
		mem("beta-gadget", "project:acme", "/work/acme"),
		mem("gamma-global", "global"),
	}
	if got := Analyze(mems); len(got) != 0 {
		t.Errorf("project scope must not produce findings; got %+v", got)
	}
}

func TestAnalyzeIsReadOnly(t *testing.T) {
	m := mem("proj-no-cwd", "project:acme")
	Analyze([]*schema.CanonicalMemory{m})
	if m.Name != "proj-no-cwd" || m.Scope != "project:acme" {
		t.Error("Analyze must not mutate the memories it inspects")
	}
}
