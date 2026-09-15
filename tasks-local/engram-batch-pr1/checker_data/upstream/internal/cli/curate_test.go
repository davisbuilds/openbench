package cli

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// fakeClaudeRunner returns a claude `--output-format json` envelope whose result
// carries the given assistant text (a fenced JSON proposal in these tests), so
// the curate loop runs end-to-end without spawning a real model.
func fakeClaudeRunner(assistantText string) func([]string) ([]byte, error) {
	return func([]string) ([]byte, error) {
		env := map[string]any{"type": "result", "is_error": false, "result": assistantText}
		b, _ := json.Marshal(env)
		return b, nil
	}
}

func curateEnv(cfg string, apply bool, runner func([]string) ([]byte, error)) *env {
	return &env{jsonMode: true, apply: apply, config: cfg, runner: runner}
}

func seedCanon(t *testing.T, canon string, names ...string) {
	t.Helper()
	for _, n := range names {
		writeFile(t, filepath.Join(canon, n+".md"),
			"---\nname: "+n+"\ndescription: d\ntype: lesson\nscope: global\n---\nbody\n")
	}
}

// TestCurateApplyAppliesValidProposal pins the proposer/applier loop: a fake
// agent proposes a merge + a remove, and --apply mutates canonical accordingly.
func TestCurateApplyAppliesValidProposal(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	seedCanon(t, canon, "dup-a", "dup-b", "stale")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\n")

	proposal := "```json\n" + `{"operations":[
	  {"op":"merge","sources":["dup-a","dup-b"],"memory":{"name":"dup","description":"d","type":"lesson","scope":"global","body":"b\n"},"reason":"same"},
	  {"op":"remove","name":"stale","reason":"old"}
	]}` + "\n```"

	defer silenceStdout(t)()
	code := cmdCurate(curateEnv(cfg, true, fakeClaudeRunner(proposal)), "curate", []string{"--harness", "claude-code"})
	if code != exitOK {
		t.Fatalf("apply exit = %d, want %d", code, exitOK)
	}
	if _, err := os.Stat(filepath.Join(canon, "dup.md")); err != nil {
		t.Errorf("merged memory dup.md missing: %v", err)
	}
	for _, gone := range []string{"dup-a", "dup-b", "stale"} {
		if _, err := os.Stat(filepath.Join(canon, gone+".md")); !os.IsNotExist(err) {
			t.Errorf("%s should have been deleted", gone)
		}
	}
}

// TestCurateFailsClosedOnInvalidOp pins the safety property: a batch with any
// invalid operation is refused whole under --apply (exit 3, nothing mutated).
func TestCurateFailsClosedOnInvalidOp(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	seedCanon(t, canon, "real")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\n")

	// One valid remove, one remove of a nonexistent memory.
	proposal := "```json\n" + `{"operations":[
	  {"op":"remove","name":"real","reason":"ok"},
	  {"op":"remove","name":"ghost","reason":"nope"}
	]}` + "\n```"

	defer silenceStdout(t)()
	code := cmdCurate(curateEnv(cfg, true, fakeClaudeRunner(proposal)), "curate", []string{"--harness", "claude-code"})
	if code != exitConflicts {
		t.Fatalf("invalid-batch apply exit = %d, want %d", code, exitConflicts)
	}
	if _, err := os.Stat(filepath.Join(canon, "real.md")); err != nil {
		t.Errorf("real.md must survive a fail-closed batch: %v", err)
	}
}

// TestCurateDryRunDoesNotMutate pins that a dry run reports a plan without
// touching canonical, even for a fully-valid proposal.
func TestCurateDryRunDoesNotMutate(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	seedCanon(t, canon, "victim")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\n")

	proposal := "```json\n" + `{"operations":[{"op":"remove","name":"victim","reason":"x"}]}` + "\n```"
	defer silenceStdout(t)()
	code := cmdCurate(curateEnv(cfg, false, fakeClaudeRunner(proposal)), "curate", []string{"--harness", "claude-code"})
	if code != exitOK {
		t.Fatalf("dry-run exit = %d, want %d", code, exitOK)
	}
	if _, err := os.Stat(filepath.Join(canon, "victim.md")); err != nil {
		t.Errorf("dry run must not delete victim.md: %v", err)
	}
}

// TestCurateAgentRunFailureSurfaces pins that a runner error becomes an exit-1
// agent_run error rather than a crash or a silent success.
func TestCurateAgentRunFailureSurfaces(t *testing.T) {
	dir := t.TempDir()
	canon := filepath.Join(dir, "canonical")
	seedCanon(t, canon, "m")
	cfg := filepath.Join(dir, "c.yaml")
	writeFile(t, cfg, "canonical_root: "+canon+"\n")

	failing := func([]string) ([]byte, error) { return nil, os.ErrPermission }
	defer silenceStdout(t)()
	code := cmdCurate(curateEnv(cfg, true, failing), "curate", []string{"--harness", "claude-code"})
	if code != exitError {
		t.Fatalf("agent run failure exit = %d, want %d", code, exitError)
	}
}
