package sync

import (
	"crypto/sha256"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/davisbuilds/engram/internal/lock"
	"github.com/davisbuilds/engram/internal/render"
	"github.com/davisbuilds/engram/internal/schema"
)

func mem(name string) *schema.CanonicalMemory {
	return &schema.CanonicalMemory{
		Name: name, Description: "desc of " + name, Type: schema.TypeLesson,
		Scope: "global", Body: "body of " + name + "\n",
	}
}

func target(dir string, mems ...*schema.CanonicalMemory) ClaudeTarget {
	return ClaudeTarget{MemoryDir: dir, Desired: mems}
}

// writeOwned simulates a prior engram render of m into dir.
func writeOwned(t *testing.T, dir string, m *schema.CanonicalMemory) {
	t.Helper()
	rr, err := render.ClaudeRenderer{}.Render(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, rr.FileName), rr.Content, 0o644); err != nil {
		t.Fatal(err)
	}
}

func kinds(as []Action) map[ActionKind]int {
	m := map[ActionKind]int{}
	for _, a := range as {
		m[a.Kind]++
	}
	return m
}

func TestApplyCreatesFiles(t *testing.T) {
	dir := t.TempDir()
	res, err := target(dir, mem("alpha-lesson"), mem("beta-lesson")).Apply()
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if len(res.Applied) != 2 {
		t.Fatalf("applied %d actions, want 2", len(res.Applied))
	}
	for _, name := range []string{"alpha-lesson", "beta-lesson"} {
		if _, err := os.Stat(filepath.Join(dir, name+".md")); err != nil {
			t.Errorf("expected %s.md to exist: %v", name, err)
		}
	}
	// index carries both anchored lines
	idx, err := os.ReadFile(filepath.Join(dir, "MEMORY.md"))
	if err != nil {
		t.Fatalf("read index: %v", err)
	}
	for _, name := range []string{"alpha-lesson", "beta-lesson"} {
		if !containsLineFor(string(idx), name) {
			t.Errorf("index missing anchored line for %s:\n%s", name, idx)
		}
	}
}

func TestApplySecondRunIsNoop(t *testing.T) {
	dir := t.TempDir()
	tg := target(dir, mem("alpha-lesson"), mem("beta-lesson"))

	res, err := tg.Apply()
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if len(res.Applied) == 0 {
		t.Fatal("first apply should have created memories (non-degenerate)")
	}
	plan, err := tg.Plan()
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	if len(plan) != 0 {
		t.Errorf("second run is not a no-op; pending actions: %v", plan)
	}
}

func TestApplyPreservesUnmarkedFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	// A hand-authored file that collides with a desired memory name.
	unmarked := []byte("---\nname: alpha-lesson\ndescription: hand written\n---\nmine\n")
	path := filepath.Join(dir, "alpha-lesson.md")
	if err := os.WriteFile(path, unmarked, 0o644); err != nil {
		t.Fatal(err)
	}
	before := sha256.Sum256(unmarked)

	tg := target(dir, mem("alpha-lesson"))
	plan, err := tg.Plan()
	if err != nil {
		t.Fatalf("Plan: %v", err)
	}
	if kinds(plan)[Conflict] != 1 {
		t.Errorf("expected 1 CONFLICT for the name collision; got %v", plan)
	}
	res, err := tg.Apply()
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if len(res.Conflicts) != 1 {
		t.Errorf("apply should report the conflict, not apply it; got %+v", res)
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("unmarked file must still exist: %v", err)
	}
	if sha256.Sum256(after) != before {
		t.Error("unmarked file was modified; it must be byte-identical")
	}
}

func TestStaleRemovesOwnedOrphanOnly(t *testing.T) {
	dir := t.TempDir()
	// An engram-owned file with no corresponding desired memory -> STALE.
	writeOwned(t, dir, mem("orphan-lesson"))
	// A hand-authored orphan -> must be left untouched.
	handPath := filepath.Join(dir, "hand-orphan.md")
	if err := os.WriteFile(handPath, []byte("---\nname: hand-orphan\ndescription: mine\n---\nx\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Desired set is empty except a new memory, so both orphans are "not desired".
	tg := target(dir, mem("keeper-lesson"))
	res, err := tg.Apply()
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if kinds(res.Applied)[Stale] != 1 {
		t.Errorf("expected exactly 1 STALE (the owned orphan); got %v", res.Applied)
	}
	if _, err := os.Stat(filepath.Join(dir, "orphan-lesson.md")); !os.IsNotExist(err) {
		t.Error("owned orphan should have been removed")
	}
	if _, err := os.Stat(handPath); err != nil {
		t.Error("hand-authored orphan must be preserved")
	}
}

func TestApplyPreservesForeignIndexLines(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	foreign := "# Memory Index\n\n- [hand](hand.md) — a human wrote this\n"
	if err := os.WriteFile(filepath.Join(dir, "MEMORY.md"), []byte(foreign), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := target(dir, mem("alpha-lesson")).Apply(); err != nil {
		t.Fatalf("Apply: %v", err)
	}
	idx, err := os.ReadFile(filepath.Join(dir, "MEMORY.md"))
	if err != nil {
		t.Fatal(err)
	}
	if !containsSub(string(idx), "- [hand](hand.md) — a human wrote this") {
		t.Errorf("foreign index line was not preserved:\n%s", idx)
	}
	if !containsLineFor(string(idx), "alpha-lesson") {
		t.Errorf("engram index line missing:\n%s", idx)
	}
}

func TestConcurrentApplyIsMutuallyExclusive(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	// Simulate a concurrent apply already holding the lock.
	release, err := lock.Acquire(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if _, err := target(dir, mem("alpha-lesson")).Apply(); err == nil {
		t.Error("apply should fail when the lock is already held")
	}
}

func containsLineFor(index, name string) bool {
	// an engram index line embeds the name anchor
	return strings.Contains(index, "engram name="+name+" -->")
}

func containsSub(s, sub string) bool { return strings.Contains(s, sub) }
