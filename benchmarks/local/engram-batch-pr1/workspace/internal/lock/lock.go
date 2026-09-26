// Package lock provides engram's exclusive apply lock via advisory file locking
// (flock). Any writer that mutates a directory of memories (a harness render
// target, or the canonical root) takes this lock so no two applies interleave.
//
// The lock is an flock held on an open file descriptor, so the kernel releases
// it automatically when the holding process exits — including on a crash. That
// removes the whole notion of a "stale" lock: there is nothing to reclaim and no
// reclaim race. A lock file left on disk by a crashed run is inert (its mere
// presence never blocks anyone); exclusivity comes from the flock, not the file.
//
// The lock is advisory and assumes a local filesystem, which engram's canonical
// root and harness homes are.
package lock

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

// Name is the lock filename created inside the guarded directory.
const Name = ".engram.lock"

// Acquire takes the exclusive lock in dir and returns a release func that frees
// it (call it, typically via defer, when the mutation completes). A second live
// holder — in this or any other process — fails immediately rather than blocking
// or interleaving.
func Acquire(dir string) (func(), error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, Name)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, fmt.Errorf("another engram apply holds the lock at %s", path)
		}
		return nil, fmt.Errorf("lock %s: %w", path, err)
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, nil
}
