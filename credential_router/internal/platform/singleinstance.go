// Package singleinstance implements the process-level single-instance guard
// used by the credential-router binary: an exclusive lock on a lock file
// plus a <pid>:<unix-seconds> pidfile that allows a later start to detect
// and reap a stale lock left behind by a dead previous process.
//
// The lock primitive is implemented per-platform so the binary works on
// Linux, macOS, and Windows without stubs:
//
//   - singleinstance_unix.go   (//go:build unix):   syscall.Flock
//   - singleinstance_windows.go (//go:build windows): windows.LockFileEx
//
// Both expose the package-private tryLock / unlock helpers used here. On
// Windows, golang.org/x/sys/windows provides LockFileEx / UnlockFileEx
// with LOCKFILE_FAIL_IMMEDIATELY|LOCKFILE_EXCLUSIVE_LOCK semantics (the
// Windows analog of flock's LOCK_EX|LOCK_NB).
//
// The functions live in an importable package (not package main) so the
// black-box tests in tests/unit/credential-router (and the white-box
// singleinstance_test.go in this package) can exercise them.
package platform

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// AcquireSingleInstanceLock takes an exclusive lock on the open lock file.
// If the lock is contended, it checks whether the previous holder left a
// stale pidfile and (if the pid is dead) reaps the lock and retries once.
func AcquireSingleInstanceLock(lockFile *os.File) error {
	return acquireSingleInstanceLock(lockFile, nil)
}

// acquireSingleInstanceLock is the testable form of AcquireSingleInstanceLock.
// beforeRetry (if non-nil) is invoked between releasing the (non-held) lock
// and the retry attempt; tests use it to synchronize with an external lock
// release so the retry can succeed deterministically across platforms.
func acquireSingleInstanceLock(lockFile *os.File, beforeRetry func()) error {
	if err := tryLock(lockFile); err == nil {
		return nil
	}
	dataDir := filepath.Dir(lockFile.Name())
	pidPath := filepath.Join(dataDir, "router.pid")
	if data, readErr := os.ReadFile(pidPath); readErr == nil {
		parts := strings.SplitN(strings.TrimSpace(string(data)), ":", 2)
		if pid, parseErr := strconv.Atoi(parts[0]); parseErr == nil {
			if proc, findErr := os.FindProcess(pid); findErr == nil {
				if killErr := proc.Signal(syscall.Signal(0)); killErr != nil {
					_ = unlock(lockFile)
					if beforeRetry != nil {
						beforeRetry()
					}
					if err := tryLock(lockFile); err == nil {
						return nil
					}
				}
			}
		}
	}
	return fmt.Errorf("another instance is running (lock held)")
}

// TryLockExclusive acquires an exclusive, non-blocking lock on f.
// Cross-platform: flock on Unix (Linux/macOS/BSD), LockFileEx on Windows.
// Exported for callers and tests that need the lock primitive without
// the reap-stale-PID path of AcquireSingleInstanceLock.
func TryLockExclusive(f *os.File) error {
	return tryLock(f)
}

// UnlockFileLock releases the lock on f. Cross-platform. Safe to call on
// a lock the caller does not hold (flock unlock is idempotent;
// UnlockFileEx is a release operation). Exported for symmetry with
// TryLockExclusive.
func UnlockFileLock(f *os.File) error {
	return unlock(f)
}

// WritePIDFile writes the "<pid>:<unix-seconds>\n" pidfile used by the
// single-instance guard. Mode 0o644 is intentional so the stale-PID
// reaping path in AcquireSingleInstanceLock can read it back on a later
// start. On Windows the mode is ignored and the file inherits its parent
// directory's NTFS ACL.
func WritePIDFile(path string, pid int) error {
	line := strconv.Itoa(pid) + ":" + strconv.FormatInt(time.Now().Unix(), 10) + "\n"
	return os.WriteFile(path, []byte(line), 0o644)
}