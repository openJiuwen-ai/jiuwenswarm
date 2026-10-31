//go:build cgo

package backup_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/store"
)

func openStoreForTest(t *testing.T) (*store.Store, func()) {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "src.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("store.OpenForTesting: %v", err)
	}
	// Insert some data so backup is non-trivial.
	ctx := context.Background()
	cred := &store.Credential{
		UserID:       "u1",
		APIBase:      "https://api.example.com",
		KeyTag:       "default",
		APIKeyCipher: []byte("encrypted_data"),
		AuthType:     "openai",
	}
	if err := s.InsertCredential(ctx, cred); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	return s, func() { _ = s.Close() }
}

func TestBackupSuccess(t *testing.T) {
	src, cleanup := openStoreForTest(t)
	defer cleanup()

	dir := t.TempDir()
	destPath := filepath.Join(dir, "backup.db")

	if err := backup.Backup(src.AdminDB(), destPath); err != nil {
		t.Fatalf("Backup: %v", err)
	}

	// Verify backup file exists and is non-empty.
	info, err := os.Stat(destPath)
	if err != nil {
		t.Fatalf("stat backup: %v", err)
	}
	if info.Size() == 0 {
		t.Error("backup file is empty")
	}

	// Verify backup contains the original credential.
	dst, err := store.OpenForTesting(destPath)
	if err != nil {
		t.Fatalf("reopen backup: %v", err)
	}
	defer dst.Close()

	cred, err := dst.GetCredentialByUserURLTag(context.Background(), "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("GetCredentialByUserURLTag on backup: %v", err)
	}
	if string(cred.APIKeyCipher) != "encrypted_data" {
		t.Errorf("backup data mismatch: got %q, want %q", cred.APIKeyCipher, "encrypted_data")
	}
}

func TestBackupNilDB(t *testing.T) {
	if err := backup.Backup(nil, filepath.Join(t.TempDir(), "x.db")); err == nil {
		t.Error("expected error for nil DB")
	}
}

func TestBackupEmptyDest(t *testing.T) {
	dir := t.TempDir()
	src, err := store.OpenForTesting(filepath.Join(dir, "src.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer src.Close()

	if err := backup.Backup(src.AdminDB(), ""); err == nil {
		t.Error("expected error for empty dest")
	}
}

func TestBackupCreatesDestDir(t *testing.T) {
	src, cleanup := openStoreForTest(t)
	defer cleanup()

	dir := t.TempDir()
	destPath := filepath.Join(dir, "subdir", "another", "backup.db")

	if err := backup.Backup(src.AdminDB(), destPath); err != nil {
		t.Fatalf("Backup: %v", err)
	}

	if _, err := os.Stat(destPath); err != nil {
		t.Errorf("backup file not created: %v", err)
	}
}
