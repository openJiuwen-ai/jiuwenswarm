//go:build cgo

package backup_test

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/store"
)

func openStoreWithLockFile(t *testing.T) (*store.Store, func()) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "src.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("store.OpenForTesting: %v", err)
	}
	ctx := context.Background()
	cred := &store.Credential{
		UserID: "u1", APIBase: "https://api.example.com", KeyTag: "default",
		APIKeyCipher: []byte("encrypted"), AuthType: "openai",
	}
	if err := s.InsertCredential(ctx, cred); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	return s, func() { _ = s.Close() }
}

func TestBackupManagerSuccess(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir, Keep: 3, KeySnapshot: backup.KeySnapshotConfig{Keep: 5}}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	for i := range s2 {
		s2[i] = byte(i + 100)
	}
	wrapped := bytes.Repeat([]byte{0xAB}, 44)

	if err := bm.Backup(context.Background(), backup.BackupKEK, backup.KeySnapshotModeAES, s1, s2, wrapped); err != nil {
		t.Fatalf("Backup: %v", err)
	}

	// Verify .db exists
	dbMatches, err := filepath.Glob(filepath.Join(dir, "backup-kek-*.db"))
	if err != nil {
		t.Fatal(err)
	}
	if len(dbMatches) != 1 {
		t.Errorf("expected 1 .db, got %d", len(dbMatches))
	}

	// Verify key snapshot exists
	scMatches, err := filepath.Glob(filepath.Join(dir, "key-snapshot-*.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if len(scMatches) != 1 {
		t.Errorf("expected 1 key snapshot, got %d", len(scMatches))
	}

	// Verify key snapshot content
	snap, err := backup.ReadKeySnapshot(scMatches[0])
	if err != nil {
		t.Fatal(err)
	}
	if snap.S1 != s1 {
		t.Error("key snapshot S1 mismatch")
	}
	if snap.S2 != s2 {
		t.Error("key snapshot S2 mismatch")
	}
	if !bytes.Equal(snap.WrappedDEK, wrapped) {
		t.Error("key snapshot wrapped_dek mismatch")
	}
	if snap.CryptoMode != backup.KeySnapshotModeAES {
		t.Errorf("key snapshot mode=0x%02x, want AES (0x%02x)", snap.CryptoMode, backup.KeySnapshotModeAES)
	}
}

func TestBackupManagerRetentionKEK(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir, Keep: 2, KeySnapshot: backup.KeySnapshotConfig{Keep: 5}}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	wrapped := bytes.Repeat([]byte{0xCD}, 44)

	// Create 4 KEK backups with small delay to ensure distinct mtimes
	for i := 0; i < 4; i++ {
		time.Sleep(10 * time.Millisecond)
		if err := bm.Backup(context.Background(), backup.BackupKEK, backup.KeySnapshotModeAES, s1, s2, wrapped); err != nil {
			t.Fatalf("Backup %d: %v", i, err)
		}
	}

	// Should retain only last 2 KEK backups (Keep=2)
	dbMatches, _ := filepath.Glob(filepath.Join(dir, "backup-kek-*.db"))
	if len(dbMatches) != 2 {
		t.Errorf("after 4 backups, expected 2 .db (keep=2), got %d", len(dbMatches))
	}

	// KeySnapshots: keep=5 so all 4 should remain
	scMatches, _ := filepath.Glob(filepath.Join(dir, "key-snapshot-*.bin"))
	if len(scMatches) != 4 {
		t.Errorf("after 4 backups, expected 4 sidecars (keep=5), got %d", len(scMatches))
	}
}

func TestBackupManagerRetentionDEK(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir, Keep: 3, KeySnapshot: backup.KeySnapshotConfig{Keep: 5}}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	wrapped := bytes.Repeat([]byte{0xCD}, 44)

	// Create 6 DEK backups
	for i := 0; i < 6; i++ {
		time.Sleep(10 * time.Millisecond)
		if err := bm.Backup(context.Background(), backup.BackupDEK, backup.KeySnapshotModeAES, s1, s2, wrapped); err != nil {
			t.Fatalf("Backup %d: %v", i, err)
		}
	}

	// Should retain only last 3 DEK backups (Keep=3)
	dbMatches, _ := filepath.Glob(filepath.Join(dir, "backup-dek-*.db"))
	if len(dbMatches) != 3 {
		t.Errorf("after 6 backups, expected 3 .db (keep=3), got %d", len(dbMatches))
	}

	// KEK backups untouched (none created)
	kekMatches, _ := filepath.Glob(filepath.Join(dir, "backup-kek-*.db"))
	if len(kekMatches) != 0 {
		t.Errorf("expected 0 KEK backups, got %d", len(kekMatches))
	}
}

func TestBackupManagerInvalidType(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	wrapped := bytes.Repeat([]byte{0xCD}, 44)

	if err := bm.Backup(context.Background(), backup.BackupType("bogus"), backup.KeySnapshotModeAES, s1, s2, wrapped); err == nil {
		t.Error("expected error for invalid type")
	}
}

func TestBackupManagerBadWrappedLen(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte

	if err := bm.Backup(context.Background(), backup.BackupKEK, backup.KeySnapshotModeAES, s1, s2, []byte("short")); err == nil {
		t.Error("expected error for short wrapped_dek")
	}
}

func TestNewBackupManagerDefaults(t *testing.T) {
	// No store needed for defaults test; use nil.
	bm, err := backup.NewBackupManager(backup.BackupConfig{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if bm.Config().Keep != 3 {
		t.Errorf("default Keep=%d, want 3", bm.Config().Keep)
	}
	if bm.Config().KeySnapshot.Keep != 5 {
		t.Errorf("default KeySnapshot.Keep=%d, want 5", bm.Config().KeySnapshot.Keep)
	}
	if bm.StoreForTesting() != nil {
		t.Error("expected nil store")
	}
}

func TestDeleteOldest(t *testing.T) {
	dir := t.TempDir()

	// Create 5 files with distinct timestamps
	for i := 0; i < 5; i++ {
		path := filepath.Join(dir, fmt.Sprintf("file-%d.txt", i))
		if err := os.WriteFile(path, []byte("data"), 0o600); err != nil {
			t.Fatal(err)
		}
		// Set mtime to be i-hours ago, so file-4.txt is oldest, file-0.txt is newest
		mtime := time.Now().Add(-time.Duration(i) * time.Hour)
		if err := os.Chtimes(path, mtime, mtime); err != nil {
			t.Fatal(err)
		}
	}

	// Keep 2 newest
	if err := backup.DeleteOldestForTesting(filepath.Join(dir, "file-*.txt"), 2); err != nil {
		t.Fatal(err)
	}

	matches, _ := filepath.Glob(filepath.Join(dir, "file-*.txt"))
	if len(matches) != 2 {
		t.Errorf("expected 2 files, got %d", len(matches))
	}

	// Verify the two newest (file-0.txt and file-1.txt) are kept
	// file-5 onwards don't exist, so we check what remains
	for _, m := range matches {
		base := filepath.Base(m)
		if base != "file-0.txt" && base != "file-1.txt" {
			t.Errorf("unexpected remaining file: %s", base)
		}
	}
}

func TestDeleteOldestKeepAll(t *testing.T) {
	dir := t.TempDir()

	for i := 0; i < 3; i++ {
		path := filepath.Join(dir, fmt.Sprintf("f-%d.txt", i))
		if err := os.WriteFile(path, []byte("data"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	// Keep more than exist — no deletion
	if err := backup.DeleteOldestForTesting(filepath.Join(dir, "f-*.txt"), 5); err != nil {
		t.Fatal(err)
	}

	matches, _ := filepath.Glob(filepath.Join(dir, "f-*.txt"))
	if len(matches) != 3 {
		t.Errorf("expected 3 files, got %d", len(matches))
	}
}

func TestDeleteOldestNoMatch(t *testing.T) {
	dir := t.TempDir()

	if err := backup.DeleteOldestForTesting(filepath.Join(dir, "nonexistent-*.txt"), 2); err != nil {
		t.Fatal(err)
	}

	matches, _ := filepath.Glob(filepath.Join(dir, "nonexistent-*.txt"))
	if len(matches) != 0 {
		t.Errorf("expected 0 files, got %d", len(matches))
	}
}

// ScanRetention must prune pre-existing over-limit files by mtime without
// any new backup being taken; no store is needed for the sweep.
func TestScanRetentionPrunesExistingFiles(t *testing.T) {
	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: dir, Keep: 2, KeySnapshot: backup.KeySnapshotConfig{Keep: 3}}, nil)
	if err != nil {
		t.Fatal(err)
	}

	// Pre-create 5 KEK backups and 7 sidecars with staggered mtimes,
	// simulating leftovers from a previous run that exited mid-backup.
	// Higher index = newer mtime (i hours ago).
	for i := 0; i < 5; i++ {
		path := filepath.Join(dir, fmt.Sprintf("backup-kek-%d.db", i))
		if err := os.WriteFile(path, []byte("data"), 0o600); err != nil {
			t.Fatal(err)
		}
		mtime := time.Now().Add(-time.Duration(5-i) * time.Hour)
		if err := os.Chtimes(path, mtime, mtime); err != nil {
			t.Fatal(err)
		}
	}
	for i := 0; i < 7; i++ {
		path := filepath.Join(dir, fmt.Sprintf("key-snapshot-%d.bin", i))
		if err := os.WriteFile(path, []byte("data"), 0o600); err != nil {
			t.Fatal(err)
		}
		mtime := time.Now().Add(-time.Duration(7-i) * time.Hour)
		if err := os.Chtimes(path, mtime, mtime); err != nil {
			t.Fatal(err)
		}
	}

	if err := bm.ScanRetention(); err != nil {
		t.Fatalf("ScanRetention: %v", err)
	}

	// Keep=2: only the 2 newest .db survive (indices 3, 4).
	dbMatches, err := filepath.Glob(filepath.Join(dir, "backup-kek-*.db"))
	if err != nil {
		t.Fatal(err)
	}
	if len(dbMatches) != 2 {
		t.Errorf("expected 2 kek .db after sweep, got %d", len(dbMatches))
	}
	for _, m := range dbMatches {
		base := filepath.Base(m)
		if base != "backup-kek-3.db" && base != "backup-kek-4.db" {
			t.Errorf("unexpected remaining kek backup: %s", base)
		}
	}

	// KeySnapshotKeep=3: only the 3 newest sidecars survive (indices 4, 5, 6).
	scMatches, err := filepath.Glob(filepath.Join(dir, "key-snapshot-*.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if len(scMatches) != 3 {
		t.Errorf("expected 3 sidecars after sweep, got %d", len(scMatches))
	}
	for _, m := range scMatches {
		base := filepath.Base(m)
		if base != "key-snapshot-4.bin" && base != "key-snapshot-5.bin" && base != "key-snapshot-6.bin" {
			t.Errorf("unexpected remaining key_snapshot: %s", base)
		}
	}

	// Spot-check the oldest files were actually removed.
	for _, gone := range []string{"backup-kek-0.db", "key-snapshot-0.bin"} {
		if _, err := os.Stat(filepath.Join(dir, gone)); !os.IsNotExist(err) {
			t.Errorf("expected %s to be pruned, stat err=%v", gone, err)
		}
	}
}
