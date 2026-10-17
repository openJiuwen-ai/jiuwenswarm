//go:build unix

package platform

import (
	"os"

	"golang.org/x/sys/unix"
)

// tryLock acquires an exclusive, non-blocking lock on f via flock(2).
// Returns unix.EWOULDBLOCK on contention. Unix-only (Linux/macOS/BSD).
//
// Uses golang.org/x/sys/unix (not stdlib syscall) so that `go mod vendor`
// picks up the x/sys/unix subpackage alongside x/sys/windows from the
// sibling singleinstance_windows.go — keeps vendor/modules.txt consistent
// across linux/darwin/windows builds.
func tryLock(f *os.File) error {
	return unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB)
}

// unlock releases the lock on f. Unix-only. Safe to call on a lock we
// don't hold (flock unlock is idempotent across non-overlapping regions).
func unlock(f *os.File) error {
	return unix.Flock(int(f.Fd()), unix.LOCK_UN)
}