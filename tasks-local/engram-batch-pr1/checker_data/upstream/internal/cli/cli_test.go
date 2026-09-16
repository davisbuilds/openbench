package cli

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/davisbuilds/engram/internal/lock"
)

// captureStdout runs fn with os.Stdout redirected and returns what it wrote.
func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	fn()
	_ = w.Close()
	os.Stdout = old
	b, _ := io.ReadAll(r)
	return string(b)
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// silenceStdout redirects command output to /dev/null for the test's duration so
// the JSON envelopes don't clutter the test log.
func silenceStdout(t *testing.T) func() {
	t.Helper()
	old := os.Stdout
	f, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = f
	return func() { os.Stdout = old; _ = f.Close() }
}

// TestRunSyncLifecycle exercises the full CLI wiring and pins the agent-first
// exit-code contract: 0 for a clean apply, 0 for an idempotent audit, 3 when an
// unmarked collision forces a CONFLICT.
func TestRunSyncLifecycle(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	claude := filepath.Join(dir, "claude")
	writeFile(t, filepath.Join(canon, "m.md"),
		"---\nname: a-mem\ndescription: d\ntype: lesson\nscope: global\n---\nbody\n")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg,
		"canonical_root: "+canon+"\nharnesses:\n  claude-code:\n    home: "+claude+"\n")

	defer silenceStdout(t)()
	base := []string{"--config", cfg, "--cwd", "/work/x", "--json"}

	if code := Run(append([]string{"sync", "--apply"}, base...)); code != exitOK {
		t.Fatalf("apply exit = %d, want %d", code, exitOK)
	}
	memFile := filepath.Join(claude, "projects", "-work-x", "memory", "a-mem.md")
	if _, err := os.Stat(memFile); err != nil {
		t.Fatalf("rendered memory file missing: %v", err)
	}

	if code := Run(append([]string{"audit"}, base...)); code != exitOK {
		t.Errorf("idempotent audit exit = %d, want %d", code, exitOK)
	}

	// A hand-authored file colliding with the desired name forces a conflict.
	writeFile(t, memFile, "---\nname: a-mem\ndescription: hand\n---\nmine\n")
	if code := Run(append([]string{"sync", "--apply"}, base...)); code != exitConflicts {
		t.Errorf("conflict apply exit = %d, want %d", code, exitConflicts)
	}
}

// TestRunRememberAndShare pins the authoring write-path: create, idempotent
// re-remember, canonical-side CONFLICT on a hand-edited file (exit 3, SC-13), and
// a scope move via share.
func TestRunRememberAndShare(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	if err := os.MkdirAll(canon, 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\n")
	defer silenceStdout(t)()
	c := []string{"--config", cfg, "--json"}
	memArgs := []string{"remember", "--name", "a-mem", "--description", "d", "--type", "lesson", "--scope", "global"}

	if code := Run(append(memArgs, c...)); code != exitOK {
		t.Fatalf("remember exit = %d, want %d", code, exitOK)
	}
	memFile := filepath.Join(canon, "a-mem.md")
	if _, err := os.Stat(memFile); err != nil {
		t.Fatalf("canonical file missing: %v", err)
	}
	if code := Run(append(memArgs, c...)); code != exitOK {
		t.Errorf("idempotent remember exit = %d, want %d", code, exitOK)
	}

	// Hand-edit to differing content, then remember without --force must conflict.
	writeFile(t, memFile, "---\nname: a-mem\ndescription: HAND\ntype: lesson\nscope: global\n---\nx\n")
	if code := Run(append(memArgs, c...)); code != exitConflicts {
		t.Errorf("conflict remember exit = %d, want %d", code, exitConflicts)
	}

	if code := Run(append([]string{"share", "a-mem", "--to", "project:acme"}, c...)); code != exitOK {
		t.Fatalf("share exit = %d, want %d", code, exitOK)
	}
	got, err := os.ReadFile(memFile)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "scope: project:acme") {
		t.Errorf("share did not update scope:\n%s", got)
	}
}

// TestRunImportClaude pins the reverse-sync wiring and the loop guard: a
// hand-authored native memory imports into canonical, while engram's own
// rendered file (origin marker) is skipped.
func TestRunImportClaude(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	claude := filepath.Join(dir, "claude")
	cmem := filepath.Join(claude, "projects", "-work-x", "memory")
	writeFile(t, filepath.Join(cmem, "human.md"),
		"---\nname: human-lesson\ndescription: d\nmetadata:\n  type: lesson\n---\nb\n")
	writeFile(t, filepath.Join(cmem, "ours.md"),
		"---\nname: our-render\ndescription: d\nmetadata:\n  type: lesson\n  origin: engram-sync\n---\nx\n")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\nharnesses:\n  claude-code:\n    home: "+claude+"\n")
	defer silenceStdout(t)()
	c := []string{"--config", cfg, "--cwd", "/work/x", "--json"}

	if code := Run(append([]string{"import", "claude-code", "--apply"}, c...)); code != exitOK {
		t.Fatalf("import exit = %d, want %d", code, exitOK)
	}
	if _, err := os.Stat(filepath.Join(canon, "human-lesson.md")); err != nil {
		t.Errorf("hand-authored memory not imported: %v", err)
	}
	if _, err := os.Stat(filepath.Join(canon, "our-render.md")); !os.IsNotExist(err) {
		t.Error("engram-origin file must be skipped by the loop guard, not imported")
	}
}

func TestRunImportDisabledHarnessIsStrict(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+filepath.Join(dir, "canonical")+"\nharnesses:\n  codex:\n    disabled: true\n")
	defer silenceStdout(t)()
	if code := Run([]string{"import", "codex", "--config", cfg, "--json"}); code != exitUsage {
		t.Errorf("import of a disabled harness exit = %d, want %d", code, exitUsage)
	}
}

func TestRunHookPrint(t *testing.T) {
	out := captureStdout(t, func() { Run([]string{"hook", "print", "--json"}) })
	if !strings.Contains(out, "engram sync --apply --quiet") {
		t.Errorf("hook fragment missing the sync command:\n%s", out)
	}
	if !strings.Contains(out, "SessionStart") || !strings.Contains(out, "Stop") {
		t.Errorf("hook fragment missing lifecycle events:\n%s", out)
	}
}

// TestRunDiffAndShow covers the read commands, including that show on a disabled
// harness is permissive (exit 0), unlike import.
func TestRunDiffAndShow(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	claude := filepath.Join(dir, "claude")
	writeFile(t, filepath.Join(canon, "m.md"),
		"---\nname: a-mem\ndescription: d\ntype: lesson\nscope: global\n---\nb\n")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+
		"\nharnesses:\n  claude-code:\n    home: "+claude+"\n  codex:\n    disabled: true\n")
	defer silenceStdout(t)()
	c := []string{"--config", cfg, "--cwd", "/work/x", "--json"}

	Run(append([]string{"sync", "--apply"}, c...))
	if code := Run(append([]string{"diff"}, c...)); code != exitOK {
		t.Errorf("diff exit = %d, want %d", code, exitOK)
	}
	if code := Run(append([]string{"show", "claude-code"}, c...)); code != exitOK {
		t.Errorf("show exit = %d, want %d", code, exitOK)
	}
	if code := Run(append([]string{"show", "codex"}, c...)); code != exitOK {
		t.Errorf("permissive show of a disabled harness exit = %d, want %d", code, exitOK)
	}
}

// TestRunConfigFlagsCodexMemoriesOff verifies the readiness check surfaces the
// silent-no-op case: engram enabled for Codex, but Codex's own memory is off.
func TestRunConfigFlagsCodexMemoriesOff(t *testing.T) {
	dir := t.TempDir()
	codex := filepath.Join(dir, "codex")
	writeFile(t, filepath.Join(codex, "config.toml"), "memories = false\n")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+filepath.Join(dir, "canonical")+
		"\nharnesses:\n  codex:\n    home: "+codex+"\n")
	out := captureStdout(t, func() { Run([]string{"config", "--config", cfg, "--json"}) })
	if !strings.Contains(out, "never consolidated") {
		t.Errorf("config should warn that Codex memory is off:\n%s", out)
	}
	if !strings.Contains(out, `"ready": false`) {
		t.Errorf("codex should report ready:false when memories is off:\n%s", out)
	}
}

func TestRunUnknownCommandIsUsageError(t *testing.T) {
	defer silenceStdout(t)()
	if code := Run([]string{"bogus", "--json"}); code != exitUsage {
		t.Errorf("unknown command exit = %d, want %d", code, exitUsage)
	}
}

// TestCanonicalMutatorsBlockUnderHeldLock pins that every canonical writer —
// remember, share, import --apply — refuses to mutate while another apply holds
// the canonical-root lock, so no writer slips past the exclusion.
func TestCanonicalMutatorsBlockUnderHeldLock(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	claude := filepath.Join(dir, "claude")
	// Seed one canonical memory so share has a target.
	writeFile(t, filepath.Join(canon, "a-mem.md"),
		"---\nname: a-mem\ndescription: d\ntype: lesson\nscope: global\n---\nb\n")
	// A hand-authored native Claude memory so import has something to write.
	writeFile(t, filepath.Join(claude, "projects", "-work-x", "memory", "human.md"),
		"---\nname: human-lesson\ndescription: d\nmetadata:\n  type: lesson\n---\nb\n")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\nharnesses:\n  claude-code:\n    home: "+claude+"\n")

	// A concurrent apply already holds the canonical lock.
	release, err := lock.Acquire(canon)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	defer silenceStdout(t)()

	cases := [][]string{
		{"remember", "--name", "new-mem", "--description", "d", "--type", "lesson", "--scope", "global"},
		{"share", "a-mem", "--to", "project:acme"},
		{"import", "claude-code", "--apply", "--cwd", "/work/x"},
	}
	for _, args := range cases {
		full := append(args, "--config", cfg, "--json")
		if code := Run(full); code != exitError {
			t.Errorf("%s under held lock: exit = %d, want %d", args[0], code, exitError)
		}
	}
	// Nothing should have been written: new-mem absent, a-mem scope unchanged.
	if _, err := os.Stat(filepath.Join(canon, "new-mem.md")); !os.IsNotExist(err) {
		t.Error("remember wrote new-mem.md despite the held lock")
	}
	got, _ := os.ReadFile(filepath.Join(canon, "a-mem.md"))
	if strings.Contains(string(got), "project:acme") {
		t.Error("share mutated a-mem despite the held lock")
	}
	if _, err := os.Stat(filepath.Join(canon, "human-lesson.md")); !os.IsNotExist(err) {
		t.Error("import wrote human-lesson.md despite the held lock")
	}
}
