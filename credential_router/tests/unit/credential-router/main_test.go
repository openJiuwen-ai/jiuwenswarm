package credential_router_test

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"credential_router/internal/platform"
)

func TestWritePIDFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "router.pid")
	before := time.Now().Unix()
	if err := platform.WritePIDFile(path, 12345); err != nil {
		t.Fatalf("writePIDFile: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	after := time.Now().Unix()
	content := strings.TrimSpace(string(data))
	parts := strings.SplitN(content, ":", 2)
	if len(parts) != 2 {
		t.Fatalf("content=%q, want <pid>:<seconds>", content)
	}
	pid, err := strconv.Atoi(parts[0])
	if err != nil || pid != 12345 {
		t.Errorf("pid=%q (err=%v), want 12345", parts[0], err)
	}
	sec, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || sec < before || sec > after {
		t.Errorf("seconds=%q (err=%v), want in [%d, %d]", parts[1], err, before, after)
	}
}

func TestAcquireSingleInstanceLockUncontended(t *testing.T) {
	dir := t.TempDir()
	lockPath := filepath.Join(dir, ".lock")
	f, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := platform.AcquireSingleInstanceLock(f); err != nil {
		t.Errorf("uncontended: %v", err)
	}
	_ = platform.UnlockFileLock(f)
}

func TestAcquireSingleInstanceLockContended(t *testing.T) {
	dir := t.TempDir()
	lockPath := filepath.Join(dir, ".lock")

	first, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if err := platform.TryLockExclusive(first); err != nil {
		t.Fatalf("acquire first: %v", err)
	}
	defer platform.UnlockFileLock(first)

	second, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if err := platform.AcquireSingleInstanceLock(second); err == nil {
		t.Error("expected error on contended lock with live holder")
	}
}

// Note: TestAcquireSingleInstanceLockReapsStalePID lives in
// internal/platform/singleinstance_test.go (white-box, same package).
// It uses the unexported acquireSingleInstanceLock(lockFile, beforeRetry)
// hook to synchronize the contended-lock release and the goroutine retry
// deterministically, without depending on Unix-only syscall.Mkfifo.
// That makes the reap path test portable to Linux, macOS, and Windows.