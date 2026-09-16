package lock

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAcquireIsExclusive(t *testing.T) {
	dir := t.TempDir()
	release, err := Acquire(dir)
	if err != nil {
		t.Fatalf("first acquire failed: %v", err)
	}
	if _, err := Acquire(dir); err == nil {
		t.Error("second acquire should fail while the lock is held")
	}
	release()
	release2, err := Acquire(dir)
	if err != nil {
		t.Fatalf("acquire after release failed: %v", err)
	}
	release2()
}

// A lock file left on disk by a crashed run is inert: because exclusivity is an
// flock the kernel already released, the mere presence of the file must never
// block a new acquirer (this is what replaces stale-lock reclaim).
func TestLeftoverLockFileDoesNotBlock(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, Name), []byte("from a dead run\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	release, err := Acquire(dir)
	if err != nil {
		t.Fatalf("a leftover lock file must not block acquisition: %v", err)
	}
	release()
}
