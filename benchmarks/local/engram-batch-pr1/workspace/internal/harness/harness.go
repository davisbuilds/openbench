// Package harness inspects whether each agent harness is actually set up to
// consume what engram writes. engram's own enable toggle says only whether engram
// should write; this package answers the separate question of whether the harness
// will read — so a user whose harness memory is off gets a warning instead of a
// silent no-op.
package harness

import (
	"os"
	"path/filepath"
	"regexp"
	"time"
)

// NativeMemory is the state of a harness's own memory feature, independent of
// engram's toggle.
type NativeMemory string

const (
	MemoryOn      NativeMemory = "on"
	MemoryOff     NativeMemory = "off"
	MemoryUnknown NativeMemory = "unknown"
)

// Report is a per-harness readiness assessment.
type Report struct {
	Harness       string       `json:"harness"`
	EngramEnabled bool         `json:"engram_enabled"`
	HomeExists    bool         `json:"home_exists"`
	NativeMemory  NativeMemory `json:"native_memory"`
	Ready         bool         `json:"ready"`
	Warnings      []string     `json:"warnings,omitempty"`
}

// CheckClaude assesses whether Claude Code will read engram's rendered memory.
// Claude Code reads project memory directories natively — there is no feature
// toggle — so readiness turns on engram's own toggle and the home existing.
func CheckClaude(home string, engramEnabled bool) Report {
	r := Report{
		Harness:       "claude-code",
		EngramEnabled: engramEnabled,
		HomeExists:    dirExists(home),
		NativeMemory:  MemoryUnknown,
	}
	if r.HomeExists {
		r.NativeMemory = MemoryOn
	}
	r.Warnings = commonWarnings(engramEnabled, home, r.HomeExists)
	r.Ready = engramEnabled && r.HomeExists
	return r
}

// CheckCodex assesses whether Codex will consolidate engram's extension notes.
// Codex only folds notes in when its own `memories` setting is on, so an off
// setting means engram's notes are written but never read.
func CheckCodex(home string, engramEnabled bool) Report {
	r := Report{
		Harness:       "codex",
		EngramEnabled: engramEnabled,
		HomeExists:    dirExists(home),
		NativeMemory:  codexMemories(home),
	}
	r.Warnings = commonWarnings(engramEnabled, home, r.HomeExists)
	switch r.NativeMemory {
	case MemoryOff:
		r.Warnings = append(r.Warnings,
			"Codex memory is off (set `memories = true` in "+filepath.Join(home, "config.toml")+
				"); engram notes will be written but never consolidated")
	case MemoryUnknown:
		r.Warnings = append(r.Warnings,
			"could not determine Codex `memories` setting in "+filepath.Join(home, "config.toml"))
	}
	if CodexMemoryStale(home) {
		r.Warnings = append(r.Warnings,
			"Codex MEMORY.md is older than 30 days; the consolidator may be stalled")
	}
	r.Ready = engramEnabled && r.HomeExists && r.NativeMemory != MemoryOff
	return r
}

// CodexMemoryStale reports whether Codex's consolidated MEMORY.md is older than
// 30 days (a sign the consolidator has not run recently). A missing file is not
// stale.
func CodexMemoryStale(home string) bool {
	fi, err := os.Stat(filepath.Join(home, "memories", "MEMORY.md"))
	if err != nil {
		return false
	}
	return time.Since(fi.ModTime()) > 30*24*time.Hour
}

func commonWarnings(engramEnabled bool, home string, homeExists bool) []string {
	var w []string
	if !engramEnabled {
		w = append(w, "engram is disabled for this harness in config")
	}
	if !homeExists {
		w = append(w, "harness home "+home+" does not exist; engram would create it, but the harness may not read from there")
	}
	return w
}

var codexMemoriesRe = regexp.MustCompile(`(?m)^\s*memories\s*=\s*(true|false)`)

func codexMemories(home string) NativeMemory {
	data, err := os.ReadFile(filepath.Join(home, "config.toml"))
	if err != nil {
		return MemoryUnknown
	}
	m := codexMemoriesRe.FindStringSubmatch(string(data))
	if m == nil {
		return MemoryUnknown
	}
	if m[1] == "true" {
		return MemoryOn
	}
	return MemoryOff
}

func dirExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}
