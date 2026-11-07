package platform

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestAcquireSingleInstanceLockReapsStalePID verifies the stale-PID reap
// path. The test holds an exclusive lock via TryLockExclusive, writes a
// pidfile naming a guaranteed-dead PID, then runs acquireSingleInstanceLock
// in a goroutine that signals just before its retry attempt; the main
// test releases the contended lock after the signal so the retry succeeds
// deterministically (works cross-platform).
func TestAcquireSingleInstanceLockReapsStalePID(t *testing.T) {
	dir := t.TempDir()
	lockPath := filepath.Join(dir, ".lock")

	// Guaranteed-dead PID: spawn a child, reap it, use its PID.
	child := exec.Command("true")
	if err := child.Run(); err != nil {
		t.Fatalf("spawn child: %v", err)
	}
	deadPID := child.Process.Pid

	// Write stale pid to a regular file (cross-platform).
	pidPath := filepath.Join(dir, "router.pid")
	if err := os.WriteFile(pidPath, []byte(fmt.Sprintf("%d:0\n", deadPID)), 0o644); err != nil {
		t.Fatalf("write stale pid: %v", err)
	}

	first, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if err := TryLockExclusive(first); err != nil {
		t.Fatalf("hold lock: %v", err)
	}

	second, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()

	reapReached := make(chan struct{})
	released := make(chan struct{})
	done := make(chan error, 1)

	go func() {
		done <- acquireSingleInstanceLock(second, func() {
			close(reapReached) // phase 1: signal main to release the contended lock
			<-released         // phase 2: block until main confirms release
		})
	}()

	<-reapReached

	if err := UnlockFileLock(first); err != nil {
		t.Fatalf("release: %v", err)
	}
	close(released)

	if err := <-done; err != nil {
		t.Errorf("acquireSingleInstanceLock with stale pid: %v, want nil", err)
	}
}