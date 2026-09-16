package harness

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func hasWarning(w []string, substr string) bool {
	for _, s := range w {
		if strings.Contains(s, substr) {
			return true
		}
	}
	return false
}

func TestCheckClaudeHomeMissingIsNotReady(t *testing.T) {
	r := CheckClaude(filepath.Join(t.TempDir(), "nope"), true)
	if r.HomeExists {
		t.Error("home should not exist")
	}
	if r.Ready {
		t.Error("a harness whose home is missing is not ready")
	}
	if !hasWarning(r.Warnings, "does not exist") {
		t.Errorf("expected a home-missing warning; got %v", r.Warnings)
	}
}

func TestCheckClaudeReady(t *testing.T) {
	home := t.TempDir()
	r := CheckClaude(home, true)
	if !r.HomeExists || r.NativeMemory != MemoryOn || !r.Ready {
		t.Errorf("expected ready claude report; got %+v", r)
	}
}

func TestCheckCodexMemoriesOffWarns(t *testing.T) {
	home := t.TempDir()
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte("model = \"x\"\nmemories = false\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := CheckCodex(home, true)
	if r.NativeMemory != MemoryOff {
		t.Errorf("native memory = %q, want off", r.NativeMemory)
	}
	if r.Ready {
		t.Error("codex with memories=false must not be ready")
	}
	if !hasWarning(r.Warnings, "consolidated") && !hasWarning(r.Warnings, "memories = true") {
		t.Errorf("expected a consolidation warning; got %v", r.Warnings)
	}
}

func TestCheckCodexMemoriesOnIsReady(t *testing.T) {
	home := t.TempDir()
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte("memories = true\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := CheckCodex(home, true)
	if r.NativeMemory != MemoryOn || !r.Ready {
		t.Errorf("expected ready codex report; got %+v", r)
	}
}

func TestCheckCodexUnknownWhenNoConfig(t *testing.T) {
	r := CheckCodex(t.TempDir(), true)
	if r.NativeMemory != MemoryUnknown {
		t.Errorf("native memory = %q, want unknown", r.NativeMemory)
	}
}

func TestCodexMemoryStale(t *testing.T) {
	home := t.TempDir()
	mem := filepath.Join(home, "memories", "MEMORY.md")
	if err := os.MkdirAll(filepath.Dir(mem), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(mem, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if CodexMemoryStale(home) {
		t.Error("a freshly written MEMORY.md is not stale")
	}
	old := time.Now().Add(-40 * 24 * time.Hour)
	if err := os.Chtimes(mem, old, old); err != nil {
		t.Fatal(err)
	}
	if !CodexMemoryStale(home) {
		t.Error("a 40-day-old MEMORY.md should be stale")
	}
}
