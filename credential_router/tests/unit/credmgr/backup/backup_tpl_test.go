//go:build cgo

package backup_test

import (
	"bytes"
	"context"
	"path/filepath"
	"testing"
	"time"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/platform"
)

// TestBackupManagerCustomTpl verifies that user-supplied filename templates
// (backup.BackupConfig.FilenameTpl and KeySnapshot.FilenameTpl) actually take effect,
// instead of the hardcoded "backup-<type>-<ts>.db" / "key-snapshot-<ts>.bin".
func TestBackupManagerCustomTpl(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{
		BackupDir:   dir,
		FilenameTpl: "custom-{type}-{ts}.backup",
		KeySnapshot: backup.KeySnapshotConfig{
			Enabled:     true,
			FilenameTpl: "custom-key_snapshot-{ts}.snap",
			Keep:        5,
		},
	}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	wrapped := bytes.Repeat([]byte{0xAB}, 44)

	if err := bm.Backup(context.Background(), backup.BackupKEK, backup.KeySnapshotModeAES, s1, s2, wrapped); err != nil {
		t.Fatalf("Backup: %v", err)
	}

	// Custom backup filename produced (not the default backup-kek-*.db).
	dbMatches, err := filepath.Glob(filepath.Join(dir, "custom-kek-*.backup"))
	if err != nil {
		t.Fatal(err)
	}
	if len(dbMatches) != 1 {
		t.Errorf("expected 1 custom .backup, got %d", len(dbMatches))
	}
	if defDB, _ := filepath.Glob(filepath.Join(dir, "backup-kek-*.db")); len(defDB) != 0 {
		t.Errorf("expected 0 default .db, got %d", len(defDB))
	}

	// Custom key snapshot filename produced (not the default key-snapshot-*.bin).
	scMatches, err := filepath.Glob(filepath.Join(dir, "custom-key_snapshot-*.snap"))
	if err != nil {
		t.Fatal(err)
	}
	if len(scMatches) != 1 {
		t.Errorf("expected 1 custom key snapshot, got %d", len(scMatches))
	}
	if defSc, _ := filepath.Glob(filepath.Join(dir, "key-snapshot-*.bin")); len(defSc) != 0 {
		t.Errorf("expected 0 default key snapshot, got %d", len(defSc))
	}
}

// TestBackupManagerRetentionCustomTpl verifies retention globs are derived from
// the template: with Keep=2 and a custom KEK template, only 2 files survive
// after 4 backups. If retention used a hardcoded "backup-kek-*.db" glob instead,
// custom template files would never match and all 4 would remain.
func TestBackupManagerRetentionCustomTpl(t *testing.T) {
	s, cleanup := openStoreWithLockFile(t)
	defer cleanup()

	dir := t.TempDir()
	bm, err := backup.NewBackupManager(backup.BackupConfig{
		BackupDir:   dir,
		Keep:        2,
		FilenameTpl: "custom-{type}-{ts}.backup",
		KeySnapshot: backup.KeySnapshotConfig{
			Keep: 10,
		},
	}, s)
	if err != nil {
		t.Fatal(err)
	}

	var s1, s2 [32]byte
	wrapped := bytes.Repeat([]byte{0xCD}, 44)

	for i := 0; i < 4; i++ {
		time.Sleep(10 * time.Millisecond)
		if err := bm.Backup(context.Background(), backup.BackupKEK, backup.KeySnapshotModeAES, s1, s2, wrapped); err != nil {
			t.Fatalf("Backup %d: %v", i, err)
		}
	}

	matches, _ := filepath.Glob(filepath.Join(dir, "custom-kek-*.backup"))
	if len(matches) != 2 {
		t.Errorf("after 4 backups, expected 2 (keep=2), got %d", len(matches))
	}
}

// TestNewBackupManagerInvalidTpl rejects non-empty templates missing {ts}.
func TestNewBackupManagerInvalidTpl(t *testing.T) {
	cases := []struct {
		name string
		cfg  backup.BackupConfig
	}{
		{"FilenameTpl missing {ts}", backup.BackupConfig{FilenameTpl: "static.db"}},
		{"KeySnapshot.FilenameTpl missing {ts}", backup.BackupConfig{KeySnapshot: backup.KeySnapshotConfig{Enabled: true, FilenameTpl: "no-ts.bin"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := backup.NewBackupManager(c.cfg, nil)
			if err == nil {
				t.Fatal("expected err for template missing {ts}, got nil")
			}
			if platform.CodeOf(err) != platform.CodeBadRequest {
				t.Errorf("err code = %v, want %v", platform.CodeOf(err), platform.CodeBadRequest)
			}
		})
	}
}

// TestNewBackupManagerDefaultsTpl verifies empty templates fall back to the
// hardcoded defaults so existing callers (and TestBackupManagerSuccess) keep
// producing backup-<type>-<ts>.db / key-snapshot-<ts>.bin.
func TestNewBackupManagerDefaultsTpl(t *testing.T) {
	bm, err := backup.NewBackupManager(backup.BackupConfig{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if bm.Config().FilenameTpl != "backup-{type}-{ts}.db" {
		t.Errorf("default FilenameTpl = %q, want backup-{type}-{ts}.db", bm.Config().FilenameTpl)
	}
	if bm.Config().KeySnapshot.FilenameTpl != "key-snapshot-{ts}.bin" {
		t.Errorf("default KeySnapshot.FilenameTpl = %q, want key-snapshot-{ts}.bin", bm.Config().KeySnapshot.FilenameTpl)
	}
}
