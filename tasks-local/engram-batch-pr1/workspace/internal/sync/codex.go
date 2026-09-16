package sync

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/render"
	"github.com/davisbuilds/engram/internal/schema"
)

const instructionsFile = "instructions.md"

// CodexTarget is engram's Codex extension directory
// (~/.codex/memories/extensions/engram) paired with the memories that should
// render into it. Notes are keyed by their marker's canonical name, not by
// filename, because each note's filename embeds the creation timestamp.
type CodexTarget struct {
	ExtensionDir string
	Desired      []*schema.CanonicalMemory
	// Now supplies the timestamp for new note filenames; nil means time.Now.
	Now func() time.Time
}

// Harness identifies this target's harness.
func (CodexTarget) Harness() string { return "codex" }

// DesiredMemories returns the scope-filtered memories for this target.
func (t CodexTarget) DesiredMemories() []*schema.CanonicalMemory { return t.Desired }

func (t CodexTarget) notesDir() string { return filepath.Join(t.ExtensionDir, "notes") }
func (t CodexTarget) instructionsPath() string {
	return filepath.Join(t.ExtensionDir, instructionsFile)
}

// codexNote is an engram-owned note already on disk.
type codexNote struct {
	content []byte
	path    string
}

// Plan computes the reconciliation actions without writing.
func (t CodexTarget) Plan() ([]Action, error) {
	owned, err := scanCodexNotes(t.notesDir())
	if err != nil {
		return nil, err
	}
	renderer := render.CodexRenderer{}
	var actions []Action

	if !fileHasContent(t.instructionsPath(), []byte(render.CodexInstructions)) {
		kind := Create
		if fileExists(t.instructionsPath()) {
			kind = Update
		}
		actions = append(actions, Action{kind, instructionsFile, t.instructionsPath(), "extension instructions"})
	}

	desired := map[string]bool{}
	for _, m := range t.Desired {
		desired[m.Name] = true
		rr, err := renderer.Render(m)
		if err != nil {
			return nil, err
		}
		cur, exists := owned[m.Name]
		switch {
		case !exists:
			actions = append(actions, Action{Create, m.Name, t.notesDir(), "new note"})
		case !bytes.Equal(cur.content, rr.Content):
			actions = append(actions, Action{Update, m.Name, cur.path, ""})
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

// Apply reconciles the Codex extension under an exclusive lock.
func (t CodexTarget) Apply() (Result, error) {
	unlock, err := acquireLock(t.ExtensionDir)
	if err != nil {
		return Result{}, err
	}
	defer unlock()

	actions, err := t.Plan()
	if err != nil {
		return Result{}, err
	}

	renderer := render.CodexRenderer{}
	rendered := map[string]render.CodexRender{}
	for _, m := range t.Desired {
		rr, err := renderer.Render(m)
		if err != nil {
			return Result{}, err
		}
		rendered[m.Name] = rr
	}
	now := t.Now
	if now == nil {
		now = time.Now
	}

	var res Result
	for _, a := range actions {
		switch {
		case a.Name == instructionsFile:
			if err := atomicWrite(t.instructionsPath(), []byte(render.CodexInstructions)); err != nil {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		case a.Kind == Create:
			fn := now().UTC().Format("2006-01-02T15-04-05") + "-" + a.Name + ".md"
			a.Path = filepath.Join(t.notesDir(), fn)
			if err := atomicWrite(a.Path, rendered[a.Name].Content); err != nil {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		case a.Kind == Update:
			if err := atomicWrite(a.Path, rendered[a.Name].Content); err != nil {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		case a.Kind == Stale:
			if err := os.Remove(a.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return res, err
			}
			res.Applied = append(res.Applied, a)
		}
	}
	return res, nil
}

// scanCodexNotes returns the engram-owned notes in dir, keyed by canonical name.
// A note without engram's marker is foreign and is left out (never touched).
func scanCodexNotes(dir string) (map[string]codexNote, error) {
	owned := map[string]codexNote{}
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return owned, nil
	}
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		content, rerr := os.ReadFile(path)
		if rerr != nil {
			return nil, rerr
		}
		if name, _, ok := marker.CodexNoteName(string(content)); ok {
			owned[name] = codexNote{content: content, path: path}
		}
	}
	return owned, nil
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func fileHasContent(path string, want []byte) bool {
	got, err := os.ReadFile(path)
	return err == nil && bytes.Equal(got, want)
}
