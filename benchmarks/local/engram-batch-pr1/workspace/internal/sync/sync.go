// Package sync reconciles canonical memories against a harness's on-disk memory
// store. It owns the filesystem, the marker-based ownership checks, atomic
// writes, an exclusive apply lock, and the four action kinds — so the renderers
// can stay pure. Two invariants are load-bearing: apply is idempotent (a second
// apply on unchanged canonical is a no-op), and a file without an engram marker
// is never modified or deleted.
package sync

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/davisbuilds/engram/internal/lock"
	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/render"
	"github.com/davisbuilds/engram/internal/schema"
)

// ActionKind is one of the reconciliation outcomes for a single memory.
type ActionKind string

const (
	// Create renders a memory that has no target file yet.
	Create ActionKind = "CREATE"
	// Update rewrites an engram-owned target whose content or index line drifted.
	Update ActionKind = "UPDATE"
	// Stale removes an engram-owned target whose canonical no longer renders here.
	Stale ActionKind = "STALE"
	// Conflict flags a target name occupied by a hand-authored (unmarked) file;
	// apply never touches it.
	Conflict ActionKind = "CONFLICT"
)

// Action is one planned change to the harness store.
type Action struct {
	Kind ActionKind `json:"kind"`
	Name string     `json:"name"`
	Path string     `json:"path"`
	Note string     `json:"note,omitempty"`
}

// Result reports what apply did.
type Result struct {
	Applied   []Action `json:"applied"`
	Conflicts []Action `json:"conflicts"`
}

// Target is one harness's reconcilable memory store. Both harness targets share
// the same plan/apply contract so the caller can drive them uniformly.
type Target interface {
	// Harness names the target harness (e.g. "claude-code", "codex").
	Harness() string
	// DesiredMemories returns the scope-filtered memories that should render here.
	DesiredMemories() []*schema.CanonicalMemory
	// Plan computes the reconciliation actions without writing.
	Plan() ([]Action, error)
	// Apply reconciles under an exclusive lock and reports what it did.
	Apply() (Result, error)
}

// ClaudeTarget is a Claude Code per-project memory directory paired with the
// memories that should render into it (already scope-filtered by the caller).
type ClaudeTarget struct {
	MemoryDir string
	Desired   []*schema.CanonicalMemory
}

// Harness identifies this target's harness.
func (ClaudeTarget) Harness() string { return "claude-code" }

// DesiredMemories returns the scope-filtered memories for this target.
func (t ClaudeTarget) DesiredMemories() []*schema.CanonicalMemory { return t.Desired }

// ownedFile is an engram-authored memory file already on disk.
type ownedFile struct {
	content []byte
	path    string
}

// Plan computes the actions needed to reconcile the target, without writing.
func (t ClaudeTarget) Plan() ([]Action, error) {
	owned, unmarked, err := scanMemoryDir(t.MemoryDir)
	if err != nil {
		return nil, err
	}
	renderer := render.ClaudeRenderer{}

	desired := map[string]bool{}
	var actions []Action
	for _, m := range t.Desired {
		desired[m.Name] = true
		rr, err := renderer.Render(m)
		if err != nil {
			return nil, err
		}
		path := filepath.Join(t.MemoryDir, rr.FileName)

		if _, isUnmarked := unmarked[m.Name]; isUnmarked {
			actions = append(actions, Action{Conflict, m.Name, path, "target exists and is not engram-owned"})
			continue
		}
		cur, exists := owned[m.Name]
		switch {
		case !exists:
			actions = append(actions, Action{Create, m.Name, path, ""})
		case !bytes.Equal(cur.content, rr.Content) || !indexInSync(t.MemoryDir, m.Name, rr.IndexLine):
			actions = append(actions, Action{Update, m.Name, path, ""})
		}
	}
	for name, cur := range owned {
		if !desired[name] {
			actions = append(actions, Action{Stale, name, cur.path, "canonical no longer renders here"})
		}
	}
	sortActions(actions)
	return actions, nil
}

// Apply reconciles the target under an exclusive lock and reports what it did.
// Conflicts are surfaced, not applied.
func (t ClaudeTarget) Apply() (Result, error) {
	unlock, err := acquireLock(t.MemoryDir)
	if err != nil {
		return Result{}, err
	}
	defer unlock()

	actions, err := t.Plan()
	if err != nil {
		return Result{}, err
	}

	renderer := render.ClaudeRenderer{}
	rendered := map[string]render.ClaudeRender{}
	for _, m := range t.Desired {
		rr, err := renderer.Render(m)
		if err != nil {
			return Result{}, err
		}
		rendered[m.Name] = rr
	}

	var res Result
	for _, a := range actions {
		switch a.Kind {
		case Conflict:
			res.Conflicts = append(res.Conflicts, a)
		case Create, Update:
			rr := rendered[a.Name]
			if err := atomicWrite(a.Path, rr.Content); err != nil {
				return res, err
			}
			if err := upsertIndexLine(t.MemoryDir, a.Name, rr.IndexLine); err != nil {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		case Stale:
			if err := os.Remove(a.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return res, err
			}
			if err := removeIndexLine(t.MemoryDir, a.Name); err != nil {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		}
	}
	return res, nil
}

// scanMemoryDir splits the memory dir into engram-owned files (by name) and
// hand-authored files (by name), ignoring the index and non-markdown entries.
func scanMemoryDir(dir string) (owned map[string]ownedFile, unmarked map[string]string, err error) {
	owned = map[string]ownedFile{}
	unmarked = map[string]string{}
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return owned, unmarked, nil
	}
	if err != nil {
		return nil, nil, err
	}
	for _, e := range entries {
		if e.IsDir() || e.Name() == "MEMORY.md" || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		content, rerr := os.ReadFile(path)
		if rerr != nil {
			return nil, nil, rerr
		}
		name := strings.TrimSuffix(e.Name(), ".md")
		if isEngramOwned(content) {
			owned[name] = ownedFile{content: content, path: path}
		} else {
			unmarked[name] = path
		}
	}
	return owned, unmarked, nil
}

// isEngramOwned reports whether a memory file carries engram's origin marker in
// its frontmatter. A file without it is hand-authored and off-limits.
func isEngramOwned(content []byte) bool {
	front := frontmatterBytes(content)
	if front == nil {
		return false
	}
	var fm struct {
		Metadata struct {
			Origin string `yaml:"origin"`
		} `yaml:"metadata"`
	}
	if err := yaml.Unmarshal(front, &fm); err != nil {
		return false
	}
	return fm.Metadata.Origin == marker.Origin
}

// frontmatterBytes returns the YAML between the opening and closing --- fences,
// or nil when the content has no frontmatter.
func frontmatterBytes(content []byte) []byte {
	s := string(content)
	if !strings.HasPrefix(s, "---\n") {
		return nil
	}
	rest := s[len("---\n"):]
	i := strings.Index(rest, "\n---\n")
	if i < 0 {
		return nil
	}
	return []byte(rest[:i])
}

func indexInSync(dir, name, want string) bool {
	cur, ok := currentIndexLine(dir, name)
	return ok && cur == want
}

func currentIndexLine(dir, name string) (string, bool) {
	for _, ln := range readIndexLines(dir) {
		if n, ok := marker.ClaudeIndexName(ln); ok && n == name {
			return ln, true
		}
	}
	return "", false
}

func indexPath(dir string) string { return filepath.Join(dir, "MEMORY.md") }

func readIndexLines(dir string) []string {
	data, err := os.ReadFile(indexPath(dir))
	if err != nil {
		return nil
	}
	s := strings.TrimSuffix(string(data), "\n")
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}

func writeIndexLines(dir string, lines []string) error {
	if len(lines) == 0 {
		if err := os.Remove(indexPath(dir)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		return nil
	}
	return atomicWrite(indexPath(dir), []byte(strings.Join(lines, "\n")+"\n"))
}

// upsertIndexLine replaces the engram line anchored to name, or appends it,
// leaving every foreign line untouched.
func upsertIndexLine(dir, name, line string) error {
	lines := readIndexLines(dir)
	for i, ln := range lines {
		if n, ok := marker.ClaudeIndexName(ln); ok && n == name {
			lines[i] = line
			return writeIndexLines(dir, lines)
		}
	}
	return writeIndexLines(dir, append(lines, line))
}

// removeIndexLine drops the engram line anchored to name, leaving foreign lines.
func removeIndexLine(dir, name string) error {
	lines := readIndexLines(dir)
	out := lines[:0]
	for _, ln := range lines {
		if n, ok := marker.ClaudeIndexName(ln); ok && n == name {
			continue
		}
		out = append(out, ln)
	}
	return writeIndexLines(dir, out)
}

// atomicWrite writes data to a temp file in the same directory and renames it
// into place, so a crash leaves the target either fully prior or fully new.
func atomicWrite(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".engram-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// acquireLock takes an exclusive apply lock on the memory dir via the shared
// lock package. A second live holder fails rather than interleaving, so no
// check-then-act window can double-write; a lock orphaned by a crash is
// reclaimed once stale.
func acquireLock(dir string) (func(), error) {
	return lock.Acquire(dir)
}

// sortActions orders actions deterministically by name then kind, so plans and
// results are stable across runs.
func sortActions(as []Action) {
	sort.Slice(as, func(i, j int) bool {
		if as[i].Name != as[j].Name {
			return as[i].Name < as[j].Name
		}
		return as[i].Kind < as[j].Kind
	})
}
