// Package store persists canonical memories. It is the write-side counterpart of
// discover: it saves a memory into the canonical root while protecting a
// hand-edited canonical file from being silently overwritten (SC-13).
package store

import (
	"bytes"
	"os"
	"path/filepath"

	"github.com/davisbuilds/engram/internal/discover"
	"github.com/davisbuilds/engram/internal/schema"
)

// Outcome describes what a Save did.
type Outcome string

const (
	Created   Outcome = "created"
	Updated   Outcome = "updated"
	Unchanged Outcome = "unchanged"
	Conflict  Outcome = "conflict"
)

// Save writes m into the canonical root. A same-name file with differing content
// is refused (Conflict) unless force is set, so engram never silently overwrites
// a hand-edited canonical memory. Identical content is a no-op (Unchanged).
func Save(root string, m *schema.CanonicalMemory, force bool) (Outcome, string, error) {
	rendered, err := m.Render()
	if err != nil {
		return "", "", err
	}
	_, path, found, err := Load(root, m.Name)
	if err != nil {
		return "", "", err
	}
	if !found {
		p := filepath.Join(root, m.Name+".md")
		if err := writeAtomic(p, rendered); err != nil {
			return "", p, err
		}
		return Created, p, nil
	}
	cur, err := os.ReadFile(path)
	if err != nil {
		return "", path, err
	}
	if bytes.Equal(cur, rendered) {
		return Unchanged, path, nil
	}
	if !force {
		return Conflict, path, nil
	}
	if err := writeAtomic(path, rendered); err != nil {
		return "", path, err
	}
	return Updated, path, nil
}

// Delete removes the canonical memory named name from the root. A missing memory
// is not an error (delete is idempotent); the bool reports whether a file was
// actually removed.
func Delete(root, name string) (bool, error) {
	_, path, found, err := Load(root, name)
	if err != nil {
		return false, err
	}
	if !found {
		return false, nil
	}
	if err := os.Remove(path); err != nil {
		return false, err
	}
	return true, nil
}

// Load returns the canonical memory named name, its file path, and whether it was
// found.
func Load(root, name string) (*schema.CanonicalMemory, string, bool, error) {
	located, _, err := discover.Locate(root)
	if err != nil {
		return nil, "", false, err
	}
	for _, l := range located {
		if l.Memory.Name == name {
			return l.Memory, l.Path, true, nil
		}
	}
	return nil, "", false, nil
}

// writeAtomic writes data via a temp file and rename, creating the directory as
// needed, so a crash leaves the target either fully prior or fully new.
func writeAtomic(path string, data []byte) error {
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
