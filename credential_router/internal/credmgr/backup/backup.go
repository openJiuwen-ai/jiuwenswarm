//go:build cgo

package backup

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/platform"
)

// BackupType distinguishes KEK vs DEK backups.
type BackupType string

const (
	BackupKEK BackupType = "kek"
	BackupDEK BackupType = "dek"
)

type KeySnapshotConfig struct {
	Enabled     bool
	FilenameTpl string
	Keep        int
}

// BackupConfig configures retention and paths.
type BackupConfig struct {
	BackupDir   string // e.g., ./backups (sibling of data_dir)
	Keep        int    // default 3 (applies to both KEK and DEK)
	FilenameTpl string // default backup-{type}-{ts}.db
	KeySnapshot KeySnapshotConfig
}

// BackupManager handles full backup + key snapshot + retention.
type BackupManager struct {
	cfg   BackupConfig
	store *store.Store
}

// NewBackupManager creates a new manager with default values for zero fields.
func NewBackupManager(cfg BackupConfig, s *store.Store) (*BackupManager, error) {
	if cfg.Keep <= 0 {
		cfg.Keep = 3
	}
	if cfg.KeySnapshot.Keep <= 0 {
		cfg.KeySnapshot.Keep = 5
	}
	if cfg.FilenameTpl == "" {
		cfg.FilenameTpl = "backup-{type}-{ts}.db"
	}
	if err := requireTpl("backup.filename_template", cfg.FilenameTpl); err != nil {
		return nil, err
	}
	if cfg.KeySnapshot.FilenameTpl == "" {
		cfg.KeySnapshot.FilenameTpl = "key-snapshot-{ts}.bin"
	}
	if err := requireTpl("backup.key_snapshot.filename_template", cfg.KeySnapshot.FilenameTpl); err != nil {
		return nil, err
	}
	return &BackupManager{cfg: cfg, store: s}, nil
}

func requireTpl(name, tpl string) error {
	if !strings.Contains(tpl, "{ts}") {
		return platform.New(platform.CodeBadRequest, "backup.requireTpl",
			fmt.Sprintf("%s %q must contain {ts}", name, tpl))
	}
	return nil
}

func tplFilename(tpl, btype string, ts int64) string {
	s := strings.ReplaceAll(tpl, "{type}", btype)
	return strings.ReplaceAll(s, "{ts}", fmt.Sprintf("%d", ts))
}

func tplGlob(tpl, btype string) string {
	s := strings.ReplaceAll(tpl, "{type}", btype)
	return strings.ReplaceAll(s, "{ts}", "*")
}

// Backup performs a full backup sequence: lock + sqlite3 backup + key snapshot + unlock + retention.
//
// btype must be BackupKEK or BackupDEK.
// mode is the crypto mode (0x01=AES, 0x02=SM).
// s1, s2 are the current key shards to embed in the key snapshot.
// wrappedDEK must be exactly 44 bytes.
//
// The backup dir is created if it does not exist.
func (bm *BackupManager) Backup(ctx context.Context, btype BackupType, mode byte, s1, s2 [crypto.ShardSize]byte, wrappedDEK []byte) error {
	return bm.backupLocked(btype, mode, s1, s2, wrappedDEK)
}

func (bm *BackupManager) Config() BackupConfig {
	return bm.cfg
}

func (bm *BackupManager) backupLocked(btype BackupType, mode byte, s1, s2 [crypto.ShardSize]byte, wrappedDEK []byte) error {
	if btype != BackupKEK && btype != BackupDEK {
		return fmt.Errorf("backup: invalid type %q", btype)
	}
	if len(wrappedDEK) != crypto.WrappedDEKSize {
		return fmt.Errorf("backup: wrapped_dek length %d, want %d", len(wrappedDEK), crypto.WrappedDEKSize)
	}

	if err := os.MkdirAll(bm.cfg.BackupDir, 0o755); err != nil {
		return fmt.Errorf("backup: create dir: %w", err)
	}

	// 2. sqlite3 online backup
	ts := time.Now().UnixMilli()
	dbName := tplFilename(bm.cfg.FilenameTpl, string(btype), ts)
	dbPath := filepath.Join(bm.cfg.BackupDir, dbName)
	if err := Backup(bm.store.AdminDB(), dbPath); err != nil {
		return fmt.Errorf("backup: sqlite: %w", err)
	}

	// 3. KeySnapshot key-snapshot
	sidecarName := tplFilename(bm.cfg.KeySnapshot.FilenameTpl, string(btype), ts)
	sidecarPath := filepath.Join(bm.cfg.BackupDir, sidecarName)
	snap := NewKeySnapshot(mode, s1, s2, wrappedDEK)
	if err := WriteKeySnapshot(sidecarPath, snap); err != nil {
		return fmt.Errorf("backup: key_snapshot: %w", err)
	}

	// 4. Retention (after success)
	if err := bm.applyRetention(); err != nil {
		return fmt.Errorf("backup: retention: %w", err)
	}
	return nil
}

// applyRetention deletes old backups beyond the keep counts.
func (bm *BackupManager) applyRetention() error {
	if err := deleteOldest(filepath.Join(bm.cfg.BackupDir, tplGlob(bm.cfg.FilenameTpl, "kek")), bm.cfg.Keep); err != nil {
		return err
	}
	if err := deleteOldest(filepath.Join(bm.cfg.BackupDir, tplGlob(bm.cfg.FilenameTpl, "dek")), bm.cfg.Keep); err != nil {
		return err
	}
	if err := deleteOldest(filepath.Join(bm.cfg.BackupDir, tplGlob(bm.cfg.KeySnapshot.FilenameTpl, "*")), bm.cfg.KeySnapshot.Keep); err != nil {
		return err
	}
	return nil
}

// ScanRetention runs retention sweep at startup, after recovery, to remove
// any stale backup files left behind by a previous run that exited mid-backup.
// Idempotent and safe to call multiple times.
func (bm *BackupManager) ScanRetention() error {
	return bm.applyRetention()
}

// deleteOldest keeps the most recent `keep` files matching the glob, deletes the rest.
// Files are sorted by modification time descending (newest first).
func deleteOldest(glob string, keep int) error {
	dir := filepath.Dir(glob)
	pattern := filepath.Base(glob)
	matches, err := filepath.Glob(filepath.Join(dir, pattern))
	if err != nil {
		return fmt.Errorf("backup: glob %s: %w", glob, err)
	}
	if len(matches) <= keep {
		return nil
	}
	// Sort by mtime descending (newest first)
	sort.Slice(matches, func(i, j int) bool {
		iInfo, iErr := os.Stat(matches[i])
		jInfo, jErr := os.Stat(matches[j])
		// If stat fails, treat as oldest.
		if iErr != nil {
			return false
		}
		if jErr != nil {
			return true
		}
		return iInfo.ModTime().After(jInfo.ModTime())
	})
	// Delete from index `keep` onward (the oldest)
	for _, path := range matches[keep:] {
		if err := os.Remove(path); err != nil {
			return fmt.Errorf("backup: remove %s: %w", path, err)
		}
	}
	return nil
}
