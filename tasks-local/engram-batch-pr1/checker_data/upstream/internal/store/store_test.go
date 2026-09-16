package store

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/davisbuilds/engram/internal/schema"
)

func newMem(name, desc, scope string) *schema.CanonicalMemory {
	return &schema.CanonicalMemory{
		Name: name, Description: desc, Type: schema.TypeLesson, Scope: scope, Body: "body\n",
	}
}

func TestSaveCreatesThenIsIdempotent(t *testing.T) {
	root := t.TempDir()
	m := newMem("a-mem", "d", "global")

	out, path, err := Save(root, m, false)
	if err != nil {
		t.Fatalf("Save: %v", err)
	}
	if out != Created {
		t.Errorf("first save outcome = %q, want created", out)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("saved file missing: %v", err)
	}

	out2, _, err := Save(root, m, false)
	if err != nil {
		t.Fatalf("second Save: %v", err)
	}
	if out2 != Unchanged {
		t.Errorf("second save outcome = %q, want unchanged (idempotent)", out2)
	}
}

func TestSaveConflictWithoutForcePreservesFile(t *testing.T) {
	root := t.TempDir()
	handPath := filepath.Join(root, "a-mem.md")
	hand := []byte("---\nname: a-mem\ndescription: hand\ntype: user\nscope: global\n---\nmine\n")
	if err := os.WriteFile(handPath, hand, 0o644); err != nil {
		t.Fatal(err)
	}

	out, _, err := Save(root, newMem("a-mem", "different", "global"), false)
	if err != nil {
		t.Fatalf("Save: %v", err)
	}
	if out != Conflict {
		t.Errorf("outcome = %q, want conflict", out)
	}
	after, _ := os.ReadFile(handPath)
	if !bytes.Equal(hand, after) {
		t.Error("conflicting canonical file must not be overwritten without force")
	}
}

func TestSaveForceOverwrites(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a-mem.md"),
		[]byte("---\nname: a-mem\ndescription: hand\ntype: user\nscope: global\n---\nmine\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	out, _, err := Save(root, newMem("a-mem", "new-desc", "global"), true)
	if err != nil {
		t.Fatalf("Save: %v", err)
	}
	if out != Updated {
		t.Errorf("outcome = %q, want updated", out)
	}
	got, _, found, err := Load(root, "a-mem")
	if err != nil {
		t.Fatal(err)
	}
	if !found || got.Description != "new-desc" {
		t.Errorf("after force overwrite: found=%v mem=%+v", found, got)
	}
}

func TestLoadNotFound(t *testing.T) {
	_, _, found, err := Load(t.TempDir(), "nope")
	if err != nil {
		t.Fatal(err)
	}
	if found {
		t.Error("Load should not find a memory in an empty root")
	}
}
