//go:build windows

package platform

import (
	"os"

	"golang.org/x/sys/windows"
)

const (
	// LOCKFILE_FAIL_IMMEDIATELY (0x0001) makes LockFileEx fail instead of
	// blocking when the region is contended — mirrors flock LOCK_NB.
	lockFileFailImmediately = 0x00000001
	// LOCKFILE_EXCLUSIVE_LOCK (0x0002) requests exclusive access — mirrors
	// flock LOCK_EX.
	lockFileExclusiveLock = 0x00000002
	// lockFileRegion is the requested lock extent (1 means "rest of file"
	// under Windows LockFileEx semantics — see MSDN).
	lockFileRegion = 1
)

// tryLock acquires an exclusive, non-blocking lock on f via LockFileEx.
// Returns ERROR_LOCK_FAILED on contention (wrapped by golang.org/x/sys).
// Windows-only.
func tryLock(f *os.File) error {
	var overlapped windows.Overlapped
	return windows.LockFileEx(
		windows.Handle(f.Fd()),
		lockFileFailImmediately|lockFileExclusiveLock,
		0, lockFileRegion, 0,
		&overlapped,
	)
}

// unlock releases the lock on f via UnlockFileEx. Safe to call on a lock
// we don't hold (UnlockFileEx is a release operation). Windows-only.
func unlock(f *os.File) error {
	var overlapped windows.Overlapped
	return windows.UnlockFileEx(
		windows.Handle(f.Fd()),
		0, lockFileRegion, 0,
		&overlapped,
	)
}