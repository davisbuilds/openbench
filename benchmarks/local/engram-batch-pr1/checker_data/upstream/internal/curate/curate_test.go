package curate

import (
	"os"
	"strings"
	"testing"

	"github.com/davisbuilds/engram/internal/lock"
	"github.com/davisbuilds/engram/internal/schema"
	"github.com/davisbuilds/engram/internal/store"
)

func mem(name string) *schema.CanonicalMemory {
	return &schema.CanonicalMemory{
		Name: name, Description: "d", Type: schema.TypeLesson, Scope: "global", Body: "b\n",
	}
}

func corpus(names ...string) []*schema.CanonicalMemory {
	out := make([]*schema.CanonicalMemory, len(names))
	for i, n := range names {
		out[i] = mem(n)
	}
	return out
}

func TestBuildPromptEmbedsCorpusAndContract(t *testing.T) {
	p, err := BuildPrompt(Corpus{Memories: corpus("alpha", "beta")})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"alpha", "beta", "```json", `"operations"`, "op"} {
		if !strings.Contains(p, want) {
			t.Errorf("prompt missing %q", want)
		}
	}
}

func TestParseProposalFencedBlock(t *testing.T) {
	text := "Here is my plan:\n```json\n{\"operations\":[{\"op\":\"remove\",\"name\":\"x\"}]}\n```\nDone."
	p, err := ParseProposal(text)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 1 || p.Operations[0].Op != "remove" || p.Operations[0].Name != "x" {
		t.Errorf("unexpected proposal: %+v", p)
	}
}

func TestParseProposalBareObjectFallback(t *testing.T) {
	text := `{"operations":[{"op":"remove","name":"y","reason":"has a } brace in a string"}]}`
	p, err := ParseProposal(text)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Operations) != 1 || p.Operations[0].Name != "y" {
		t.Errorf("unexpected proposal: %+v", p)
	}
}

func TestParseProposalRejectsNoJSON(t *testing.T) {
	if _, err := ParseProposal("I could not decide, sorry."); err == nil {
		t.Error("expected an error when no JSON is present")
	}
}

func TestValidateAcceptsGoodOps(t *testing.T) {
	c := corpus("dup-one", "dup-two", "keep", "widen")
	ops := []Operation{
		{Op: OpMerge, Sources: []string{"dup-one", "dup-two"}, Memory: mem("dup-merged")},
		{Op: OpRemove, Name: "keep"},
		{Op: OpRescope, Name: "widen", ToScope: "project:acme"},
		{Op: OpAdd, Memory: mem("brand-new")},
	}
	results := Validate(ops, c)
	if !AllValid(results) {
		for _, r := range results {
			if !r.Valid {
				t.Errorf("op %+v invalid: %s", r.Op, r.Error)
			}
		}
	}
}

func TestValidateRejectsBadOps(t *testing.T) {
	c := corpus("real")
	cases := []struct {
		name string
		op   Operation
	}{
		{"unknown op", Operation{Op: "frobnicate", Name: "real"}},
		{"remove missing", Operation{Op: OpRemove, Name: "ghost"}},
		{"add existing", Operation{Op: OpAdd, Memory: mem("real")}},
		{"add invalid memory", Operation{Op: OpAdd, Memory: &schema.CanonicalMemory{Name: "Bad Name"}}},
		{"update rename", Operation{Op: OpUpdate, Name: "real", Memory: mem("renamed")}},
		{"merge one source", Operation{Op: OpMerge, Sources: []string{"real"}, Memory: mem("m")}},
		{"merge ghost source", Operation{Op: OpMerge, Sources: []string{"real", "ghost"}, Memory: mem("m")}},
		{"rescope bad scope", Operation{Op: OpRescope, Name: "real", ToScope: "nonsense"}},
	}
	for _, tc := range cases {
		results := Validate([]Operation{tc.op}, c)
		if results[0].Valid {
			t.Errorf("%s: expected invalid, got valid", tc.name)
		}
	}
}

func TestApplyFailsClosedOnInvalidBatch(t *testing.T) {
	root := t.TempDir()
	seed(t, root, mem("real"))
	// One good op, one bad op: nothing should be applied.
	ops := []Operation{
		{Op: OpRemove, Name: "real"},
		{Op: OpRemove, Name: "ghost"},
	}
	if _, err := Apply(root, ops); err == nil {
		t.Fatal("expected Apply to refuse an invalid batch")
	}
	if _, _, found, _ := store.Load(root, "real"); !found {
		t.Error("real was deleted despite a fail-closed batch")
	}
}

func TestApplyMergeWritesAndDeletesSources(t *testing.T) {
	root := t.TempDir()
	c := corpus("a", "b")
	seed(t, root, c...)
	ops := []Operation{
		{Op: OpMerge, Sources: []string{"a", "b"}, Memory: mem("ab")},
	}
	applied, err := Apply(root, ops)
	if err != nil {
		t.Fatal(err)
	}
	if len(applied) != 1 || applied[0].Name != "ab" {
		t.Fatalf("unexpected applied: %+v", applied)
	}
	if _, _, found, _ := store.Load(root, "ab"); !found {
		t.Error("merged memory 'ab' was not written")
	}
	for _, gone := range []string{"a", "b"} {
		if _, _, found, _ := store.Load(root, gone); found {
			t.Errorf("source %q should have been deleted", gone)
		}
	}
}

func TestApplyMergeKeepsSourceReusedAsTarget(t *testing.T) {
	root := t.TempDir()
	c := corpus("a", "b")
	seed(t, root, c...)
	// Merged memory reuses source name "a": a stays, b is deleted.
	ops := []Operation{
		{Op: OpMerge, Sources: []string{"a", "b"}, Memory: mem("a")},
	}
	if _, err := Apply(root, ops); err != nil {
		t.Fatal(err)
	}
	if _, _, found, _ := store.Load(root, "a"); !found {
		t.Error("reused source 'a' should be kept")
	}
	if _, _, found, _ := store.Load(root, "b"); found {
		t.Error("source 'b' should be deleted")
	}
}

func TestApplyRescopeChangesScope(t *testing.T) {
	root := t.TempDir()
	c := corpus("m")
	seed(t, root, c...)
	ops := []Operation{{Op: OpRescope, Name: "m", ToScope: "project:acme"}}
	if _, err := Apply(root, ops); err != nil {
		t.Fatal(err)
	}
	got, _, _, _ := store.Load(root, "m")
	if got.Scope != "project:acme" {
		t.Errorf("scope = %q, want project:acme", got.Scope)
	}
}

func TestApplyBlocksWhenLockHeld(t *testing.T) {
	root := t.TempDir()
	c := corpus("victim")
	seed(t, root, c...)
	// A concurrent apply already holds the lock on the canonical root.
	release, err := lock.Acquire(root)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	ops := []Operation{{Op: OpRemove, Name: "victim"}}
	if _, err := Apply(root, ops); err == nil {
		t.Fatal("Apply should fail while the canonical lock is held")
	}
	// The batch must not have run: victim survives.
	if _, _, found, _ := store.Load(root, "victim"); !found {
		t.Error("victim was deleted despite a held lock")
	}
}

// P1-2: a merge whose target names an existing, unrelated memory must be
// rejected — applying it would force-overwrite that memory.
func TestValidateRejectsMergeOntoUnrelatedTarget(t *testing.T) {
	c := corpus("a", "b", "important")
	ops := []Operation{{Op: OpMerge, Sources: []string{"a", "b"}, Memory: mem("important")}}
	if Validate(ops, c)[0].Valid {
		t.Error("merge onto an existing unrelated memory must be invalid")
	}
	// But merging into a brand-new name, or reusing a source name, is fine.
	for _, target := range []string{"a", "brand-new"} {
		ops := []Operation{{Op: OpMerge, Sources: []string{"a", "b"}, Memory: mem(target)}}
		if !Validate(ops, c)[0].Valid {
			t.Errorf("merge into %q should be valid", target)
		}
	}
}

// P1-4: operations in one batch are validated against the state earlier ops
// would produce, so two adds of the same new name cannot both pass.
func TestValidateCatchesIntraBatchConflicts(t *testing.T) {
	c := corpus("existing")
	// Two adds of the same previously-absent name.
	dup := []Operation{
		{Op: OpAdd, Memory: mem("new-one")},
		{Op: OpAdd, Memory: mem("new-one")},
	}
	res := Validate(dup, c)
	if !res[0].Valid || res[1].Valid {
		t.Errorf("first add should pass, second (duplicate) should fail: %+v", res)
	}
	// Remove a memory, then update the same name: the update must fail closed.
	ro := []Operation{
		{Op: OpRemove, Name: "existing"},
		{Op: OpUpdate, Name: "existing", Memory: mem("existing")},
	}
	res = Validate(ro, c)
	if !res[0].Valid || res[1].Valid {
		t.Errorf("remove-then-update of the same name must reject the update: %+v", res)
	}
}

// P1-3: Apply re-discovers and re-validates against current canonical under the
// lock, so a proposal built on a since-changed snapshot fails closed rather than
// force-writing stale output.
func TestApplyRevalidatesAgainstCurrentDisk(t *testing.T) {
	root := t.TempDir()
	seed(t, root, mem("target"))
	// The agent proposed a rescope of "target" against the snapshot it saw...
	ops := []Operation{{Op: OpRescope, Name: "target", ToScope: "project:acme"}}
	// ...but another writer removed "target" before we apply.
	if _, err := store.Delete(root, "target"); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(root, ops); err == nil {
		t.Error("Apply must fail closed when the proposal no longer validates against current canonical")
	}
}

// A batch that updates a memory and then rescopes it must keep the updated
// content: the rescope reads current on-disk state, not the initial snapshot.
func TestApplyUpdateThenRescopeKeepsUpdatedContent(t *testing.T) {
	root := t.TempDir()
	seed(t, root, mem("x"))
	updated := &schema.CanonicalMemory{
		Name: "x", Description: "updated", Type: schema.TypeLesson, Scope: "global", Body: "new body\n",
	}
	ops := []Operation{
		{Op: OpUpdate, Name: "x", Memory: updated},
		{Op: OpRescope, Name: "x", ToScope: "project:acme"},
	}
	if _, err := Apply(root, ops); err != nil {
		t.Fatal(err)
	}
	got, _, _, _ := store.Load(root, "x")
	if got.Body != "new body\n" {
		t.Errorf("rescope discarded the prior update: body = %q", got.Body)
	}
	if got.Scope != "project:acme" {
		t.Errorf("scope = %q, want project:acme", got.Scope)
	}
}

// Apply must refuse to mutate a canonical store that contains an unparseable
// file, since such a file is invisible to validation and could be clobbered.
func TestApplyRefusesWhenCanonicalHasParseError(t *testing.T) {
	root := t.TempDir()
	seed(t, root, mem("ok"))
	// A malformed memory file (no frontmatter) sits in the canonical root.
	if err := os.WriteFile(root+"/broken.md", []byte("no frontmatter here\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ops := []Operation{{Op: OpAdd, Memory: mem("addition")}}
	if _, err := Apply(root, ops); err == nil {
		t.Error("Apply must fail closed when a canonical file does not parse")
	}
	if _, _, found, _ := store.Load(root, "addition"); found {
		t.Error("nothing should have been written while canonical had a parse error")
	}
}

func seed(t *testing.T, root string, mems ...*schema.CanonicalMemory) {
	t.Helper()
	for _, m := range mems {
		if _, _, err := store.Save(root, m, true); err != nil {
			t.Fatalf("seed %s: %v", m.Name, err)
		}
	}
	// sanity: files exist
	if entries, _ := os.ReadDir(root); len(entries) != len(mems) {
		t.Fatalf("seed wrote %d files, want %d", len(entries), len(mems))
	}
}
