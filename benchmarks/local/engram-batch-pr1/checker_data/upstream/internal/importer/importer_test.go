package importer

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/davisbuilds/engram/internal/schema"
)

func byName(mems []*schema.CanonicalMemory) map[string]*schema.CanonicalMemory {
	m := map[string]*schema.CanonicalMemory{}
	for _, x := range mems {
		m[x.Name] = x
	}
	return m
}

func TestImportClaudeMapsNativeFrontmatter(t *testing.T) {
	dir := t.TempDir()
	// A hand-authored native Claude memory (type under metadata, no origin).
	native := "---\nname: rg-gotcha\ndescription: never rg -r\nmetadata:\n  type: lesson\n---\nBody stays.\n"
	if err := os.WriteFile(filepath.Join(dir, "rg-gotcha.md"), []byte(native), 0o644); err != nil {
		t.Fatal(err)
	}
	// MEMORY.md index must be ignored.
	if err := os.WriteFile(filepath.Join(dir, "MEMORY.md"), []byte("- [x](x.md)\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// cwd is a bare temp dir with no .git above it → global scope.
	res, err := ImportClaude(dir, dir)
	if err != nil {
		t.Fatalf("ImportClaude: %v", err)
	}
	if len(res.Memories) != 1 {
		t.Fatalf("got %d memories, want 1", len(res.Memories))
	}
	m := res.Memories[0]
	if m.Name != "rg-gotcha" || string(m.Type) != "lesson" || m.Description != "never rg -r" {
		t.Errorf("mapping wrong: %+v", m)
	}
	if m.Scope != "global" {
		t.Errorf("imported scope = %q, want global (no repo above cwd)", m.Scope)
	}
	if m.Body != "Body stays.\n" {
		t.Errorf("body not preserved: %q", m.Body)
	}
}

func TestImportClaudeSkipsEngramOrigin(t *testing.T) {
	dir := t.TempDir()
	ours := "---\nname: ours\ndescription: d\nmetadata:\n  type: lesson\n  origin: engram-sync\n---\nx\n"
	if err := os.WriteFile(filepath.Join(dir, "ours.md"), []byte(ours), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := ImportClaude(dir, dir)
	if err != nil {
		t.Fatalf("ImportClaude: %v", err)
	}
	if len(res.Memories) != 0 {
		t.Errorf("engram-origin file must not be imported; got %d", len(res.Memories))
	}
	if len(res.Skipped) != 1 {
		t.Errorf("expected 1 loop-guard skip, got %d", len(res.Skipped))
	}
}

func TestImportCodexSplitsTaskGroups(t *testing.T) {
	dir := t.TempDir()
	mem := "# Task Group: Dotfiles / Tokyo Night theme\n\nscope: apply a theme\n\n## Task 1\n\n" +
		"# Task Group: Supabase migration replay CI\n\ndetails here\n"
	path := filepath.Join(dir, "MEMORY.md")
	if err := os.WriteFile(path, []byte(mem), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := ImportCodex(path)
	if err != nil {
		t.Fatalf("ImportCodex: %v", err)
	}
	if len(res.Memories) != 2 {
		t.Fatalf("got %d memories, want 2 (one per Task Group)", len(res.Memories))
	}
	names := byName(res.Memories)
	if _, ok := names["dotfiles-tokyo-night-theme"]; !ok {
		t.Errorf("expected slugified name; got %v", keys(names))
	}
}

func TestImportCodexSkipsEngramGroups(t *testing.T) {
	dir := t.TempDir()
	// A consolidated Task Group carrying engram's own marker must be skipped.
	mem := "# Task Group: real user lesson\n\ndetails\n\n" +
		"# Task Group: engram note fold-in\n\n<!-- engram-sync canonical=x scope=global extension=engram -->\nfolded\n"
	path := filepath.Join(dir, "MEMORY.md")
	if err := os.WriteFile(path, []byte(mem), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := ImportCodex(path)
	if err != nil {
		t.Fatalf("ImportCodex: %v", err)
	}
	if len(res.Memories) != 1 {
		t.Errorf("engram-origin Task Group must be skipped; imported %d", len(res.Memories))
	}
	if len(res.Skipped) != 1 {
		t.Errorf("expected 1 loop-guard skip, got %d", len(res.Skipped))
	}
}

func TestImportCodexStaleWarning(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "MEMORY.md")
	if err := os.WriteFile(path, []byte("# Task Group: a lesson\n\nbody\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if res, _ := ImportCodex(path); res.StaleWarning {
		t.Error("a fresh MEMORY.md must not be flagged stale")
	}
	old := time.Now().Add(-40 * 24 * time.Hour)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	res, err := ImportCodex(path)
	if err != nil {
		t.Fatal(err)
	}
	if !res.StaleWarning {
		t.Error("a 40-day-old MEMORY.md should set StaleWarning")
	}
}

func TestSlugify(t *testing.T) {
	cases := map[string]string{
		"Dotfiles / Tokyo Night theme": "dotfiles-tokyo-night-theme",
		"  Trailing/leading  ":         "trailing-leading",
		"Already-kebab-2":              "already-kebab-2",
	}
	for in, want := range cases {
		if got := slugify(in); got != want {
			t.Errorf("slugify(%q) = %q, want %q", in, got, want)
		}
	}
}

func keys(m map[string]*schema.CanonicalMemory) []string {
	var ks []string
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}

func TestDeriveClaudeScopeRepoRootBecomesProject(t *testing.T) {
	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	mdir := filepath.Join(repo, ".claude", "memory")
	if err := os.MkdirAll(mdir, 0o755); err != nil {
		t.Fatal(err)
	}
	native := "---\nname: repo-lesson\ndescription: d\nmetadata:\n  type: lesson\n---\nb\n"
	if err := os.WriteFile(filepath.Join(mdir, "repo-lesson.md"), []byte(native), 0o644); err != nil {
		t.Fatal(err)
	}
	// cwd is a subdirectory inside the repo: scope resolves to the repo's base.
	res, err := ImportClaude(mdir, filepath.Join(repo, "src", "pkg"))
	if err != nil {
		t.Fatal(err)
	}
	// The subdir does not exist on disk, but the walk finds .git at repo root.
	want := "project:" + filepath.Base(repo)
	if len(res.Memories) != 1 || res.Memories[0].Scope != want {
		t.Errorf("scope = %q, want %q", res.Memories[0].Scope, want)
	}
}

// mkRepo makes a temp dir named <name> containing a .git marker and returns its
// path, so scope derivation (which requires a real repo) has something to find.
func mkRepo(t *testing.T, name string) string {
	t.Helper()
	repo := filepath.Join(t.TempDir(), name)
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	return repo
}

func TestDeriveCodexScopeFromRealRepo(t *testing.T) {
	repo := mkRepo(t, "dotfiles")
	got := deriveCodexScope("applies_to: cwd=" + repo + "; reuse_rule=x\nmore body")
	if got != "project:dotfiles" {
		t.Errorf("got %q, want project:dotfiles", got)
	}
	// Only the base name enters the scope; the full path never does.
	if strings.Contains(got, string(filepath.Separator)) {
		t.Errorf("scope %q leaked a path separator", got)
	}
}

func TestDeriveCodexScopeNonRepoPathStaysGlobal(t *testing.T) {
	// A container directory (no .git) must not become a project scope, and a
	// group with no applies_to line stays global.
	container := t.TempDir() // exists but has no .git
	if got := deriveCodexScope("applies_to: cwd=" + container + "\n"); got != "global" {
		t.Errorf("non-repo cwd: got %q, want global", got)
	}
	if got := deriveCodexScope("scope: apply a theme\n## Task 1\n"); got != "global" {
		t.Errorf("no applies_to: got %q, want global", got)
	}
}

func TestProjectScopeFromRepoGuardsDegenerate(t *testing.T) {
	// "." is not degenerate — it resolves to the real cwd (covered separately);
	// these are the paths that must always yield global.
	for _, p := range []string{"", "/", "  "} {
		if got := projectScopeFromRepo(p); got != "global" {
			t.Errorf("projectScopeFromRepo(%q) = %q, want global", p, got)
		}
	}
}

func TestImportCodexDerivesProjectScope(t *testing.T) {
	repo := mkRepo(t, "dotfiles")
	dir := t.TempDir()
	mem := "# Task Group: Dotfiles theme\n\n" +
		"applies_to: cwd=" + repo + "; reuse_rule=x\n\n## Task 1\nbody\n"
	path := filepath.Join(dir, "MEMORY.md")
	if err := os.WriteFile(path, []byte(mem), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := ImportCodex(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Memories) != 1 || res.Memories[0].Scope != "project:dotfiles" {
		t.Errorf("scope = %q, want project:dotfiles", res.Memories[0].Scope)
	}
}

func TestProjectScopeFromRepoResolvesRelativeCwd(t *testing.T) {
	repo := mkRepo(t, "myrepo")
	t.Chdir(repo) // run as if invoked from inside the repo
	// A relative "." must resolve to the absolute repo root, not "project:.".
	if got := projectScopeFromRepo("."); got != "project:myrepo" {
		t.Errorf("projectScopeFromRepo(\".\") = %q, want project:myrepo", got)
	}
}
