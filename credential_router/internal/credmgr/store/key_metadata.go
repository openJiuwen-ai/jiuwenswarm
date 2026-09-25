//go:build cgo

package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/platform"
)

const KeyMetadataID = int64(1)

// ErrKeyMetadataEmpty is returned by GetKeyMetadata when no key_metadata row
// exists. Callers use errors.Is to distinguish this from other not-found cases
// (e.g. self-init vs. credential lookup).
var ErrKeyMetadataEmpty = errors.New("store: key_metadata is empty")

// KeyMetadata holds the single-row encryption metadata for the store.
type KeyMetadata struct {
	ActiveKekVersion   int64
	PendingKekVersion  int64
	ActiveDekVersion   int64
	PendingDekVersion  int64
	CryptoMode         crypto.Mode
	ActiveConfigShard  []byte
	PendingConfigShard []byte
	FileShardVersion   int64
	FileShardRotatedAt int64
	LastRotateAt       int64
	WrappedDEK         []byte
	PendingWrappedDEK  []byte
	DekRotatedAt       int64
	UpdatedAt          int64
}

// GetKeyMetadata returns the single key_metadata row, or platform.ErrNotFound if empty.
func (s *Store) GetKeyMetadata(ctx context.Context) (*KeyMetadata, error) {
	row := s.proxyDB.QueryRowContext(ctx, `
		SELECT active_kek_version, pending_kek_version, active_dek_version, pending_dek_version,
		       crypto_mode, active_config_shard, pending_config_shard, file_shard_version,
		       file_shard_rotated_at, last_rotate_at, wrapped_dek, pending_wrapped_dek,
		       dek_rotated_at, updated_at
		FROM key_metadata WHERE id = ?`, KeyMetadataID)
	var km KeyMetadata
	var modeStr string
	err := row.Scan(
		&km.ActiveKekVersion, &km.PendingKekVersion, &km.ActiveDekVersion, &km.PendingDekVersion,
		&modeStr, &km.ActiveConfigShard, &km.PendingConfigShard, &km.FileShardVersion,
		&km.FileShardRotatedAt, &km.LastRotateAt, &km.WrappedDEK, &km.PendingWrappedDEK,
		&km.DekRotatedAt, &km.UpdatedAt,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, fmt.Errorf("%w: %w", platform.ErrNotFound, ErrKeyMetadataEmpty)
		}
		return nil, fmt.Errorf("store: get key_metadata: %w", err)
	}
	mode, err := crypto.ParseMode(modeStr)
	if err != nil {
		return nil, fmt.Errorf("store: parse crypto_mode %q: %w", modeStr, err)
	}
	km.CryptoMode = mode
	return &km, nil
}

// InsertKeyMetadata inserts the initial row (id=1). Returns platform.ErrConflict if already exists.
func (s *Store) InsertKeyMetadata(ctx context.Context, km *KeyMetadata) error {
	if km == nil {
		return fmt.Errorf("store: nil key_metadata")
	}
	now := time.Now().Unix()
	_, err := s.adminDB.ExecContext(ctx, `
		INSERT INTO key_metadata (
			id, active_kek_version, pending_kek_version, active_dek_version, pending_dek_version,
			crypto_mode, active_config_shard, pending_config_shard, file_shard_version,
			file_shard_rotated_at, last_rotate_at, wrapped_dek, pending_wrapped_dek,
			dek_rotated_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		KeyMetadataID,
		km.ActiveKekVersion, km.PendingKekVersion, km.ActiveDekVersion, km.PendingDekVersion,
		km.CryptoMode.String(), km.ActiveConfigShard, km.PendingConfigShard, km.FileShardVersion,
		km.FileShardRotatedAt, km.LastRotateAt, km.WrappedDEK, km.PendingWrappedDEK,
		km.DekRotatedAt, now,
	)
	if err != nil {
		if isUniquenessConstraintError(err) {
			return fmt.Errorf("%w: key_metadata already exists", platform.ErrConflict)
		}
		return fmt.Errorf("store: insert key_metadata: %w", err)
	}
	km.UpdatedAt = now
	return nil
}

// UpdateKeyMetadata updates the single key_metadata row. updated_at is auto-set.
// Returns platform.ErrNotFound if the row is missing.
func (s *Store) UpdateKeyMetadata(ctx context.Context, km *KeyMetadata) error {
	if km == nil {
		return fmt.Errorf("store: nil key_metadata")
	}
	now := time.Now().Unix()
	res, err := s.adminDB.ExecContext(ctx, `
		UPDATE key_metadata SET
			active_kek_version=?, pending_kek_version=?, active_dek_version=?, pending_dek_version=?,
			crypto_mode=?, active_config_shard=?, pending_config_shard=?, file_shard_version=?,
			file_shard_rotated_at=?, last_rotate_at=?, wrapped_dek=?, pending_wrapped_dek=?,
			dek_rotated_at=?, updated_at=?
		WHERE id=?`,
		km.ActiveKekVersion, km.PendingKekVersion, km.ActiveDekVersion, km.PendingDekVersion,
		km.CryptoMode.String(), km.ActiveConfigShard, km.PendingConfigShard, km.FileShardVersion,
		km.FileShardRotatedAt, km.LastRotateAt, km.WrappedDEK, km.PendingWrappedDEK,
		km.DekRotatedAt, now, KeyMetadataID,
	)
	if err != nil {
		return fmt.Errorf("store: update key_metadata: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: rows affected: %w", err)
	}
	if n == 0 {
		return fmt.Errorf("%w: key_metadata row missing", platform.ErrNotFound)
	}
	km.UpdatedAt = now
	return nil
}

// BulkUpdateKekVersion updates kek_version for all credentials where kek_version < newVersion.
// Returns the number of rows updated.
func (s *Store) BulkUpdateKekVersion(ctx context.Context, newVersion int64) (int64, error) {
	res, err := s.adminDB.ExecContext(ctx,
		`UPDATE credentials SET kek_version=?, row_version=row_version+1, updated_at=? WHERE kek_version < ?`,
		newVersion, time.Now().Unix(), newVersion)
	if err != nil {
		return 0, fmt.Errorf("store: bulk update kek_version: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: rows affected: %w", err)
	}
	return n, nil
}

// ReencryptDekVersionBatch re-encrypts up to `limit` credentials whose dek_version <
// targetDek: decrypt each row's api_key_cipher with oldDEK, re-encrypt with newDEK using
// the given mode, and update the row's cipher + dek_version in a single transaction.
//
// Concurrent admin CRUD may rewrite a row between this function's SELECT and its
// per-row UPDATE. To avoid silently overwriting the CRUD's newer plaintext with
// a re-encryption of the stale value, the UPDATE carries a row_version guard
// matching the version that was read; if the row has advanced (a CRUD update
// or delete), the UPDATE matches 0 rows and that row is silently skipped
// (not overwritten).
//
// Returns the count of rows re-encrypted (0 means no rows with dek_version < targetDek).
//
// Caller is responsible for retrying until 0 is returned (Phase A loop pattern).
func (s *Store) ReencryptDekVersionBatch(ctx context.Context, oldDEK, newDEK []byte, mode crypto.Mode, targetDek int64, limit int64) (int64, error) {
	if limit <= 0 {
		return 0, fmt.Errorf("store: limit must be positive, got %d", limit)
	}
	if len(oldDEK) == 0 || len(newDEK) == 0 {
		return 0, fmt.Errorf("store: oldDEK/newDEK empty")
	}

	tx, err := s.adminDB.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("store: begin tx: %w", err)
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(ctx,
		`SELECT id, row_version, api_key_cipher FROM credentials WHERE dek_version < ? ORDER BY id LIMIT ?`,
		targetDek, limit)
	if err != nil {
		return 0, fmt.Errorf("store: select phase A batch: %w", err)
	}

	type pending struct {
		id        int64
		rowVer    int64
		newCipher []byte
	}
	var batch []pending
	for rows.Next() {
		var id, rowVer int64
		var cipher []byte
		if err := rows.Scan(&id, &rowVer, &cipher); err != nil {
			rows.Close()
			return 0, fmt.Errorf("store: scan phase A: %w", err)
		}
		plaintext, err := crypto.DecryptCredential(mode, oldDEK, cipher)
		if err != nil {
			rows.Close()
			return 0, fmt.Errorf("%w: row %d: %v", ErrPhaseADecryptFail, id, err)
		}
		newCipher, err := crypto.EncryptCredential(mode, newDEK, plaintext)
		if err != nil {
			rows.Close()
			return 0, fmt.Errorf("store: re-encrypt row %d: %v", id, err)
		}
		for i := range plaintext {
			plaintext[i] = 0
		}
		batch = append(batch, pending{id: id, rowVer: rowVer, newCipher: newCipher})
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, fmt.Errorf("store: rows iter: %w", err)
	}
	rows.Close()

	// Test-only deterministic barrier between SELECT and UPDATE: lets a
	// concurrent admin UpdateCredential win the race so the row_version guard
	// in the UPDATE proves we do not silently overwrite newer data.
	// No-op in production (the global is nil when not built with the `test` tag).
	if reencryptPhaseHook != nil {
		reencryptPhaseHook(ctx)
	}

	if len(batch) == 0 {
		_ = tx.Commit()
		return 0, nil
	}

	stmt, err := tx.PrepareContext(ctx,
		`UPDATE credentials SET api_key_cipher=?, dek_version=?, row_version=row_version+1, updated_at=? WHERE id=? AND row_version=?`)
	if err != nil {
		return 0, fmt.Errorf("store: prepare update: %w", err)
	}
	now := time.Now().Unix()
	for _, p := range batch {
		res, err := stmt.ExecContext(ctx, p.newCipher, targetDek, now, p.id, p.rowVer)
		if err != nil {
			stmt.Close()
			return 0, fmt.Errorf("store: update row %d: %w", p.id, err)
		}
		// RowsAffected==0 means a concurrent admin write advanced row_version
		// after our SELECT. The row is now already in target state
		// (dek_version>=targetDek) and must not be treated as a straggler.
		// We silently skip; CountStragglersByDekVersion will not see it.
		_, _ = res.RowsAffected()
	}
	stmt.Close()

	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("store: commit phase A tx: %w", err)
	}
	return int64(len(batch)), nil
}

// reencryptPhaseHook is the test-only hook fired between Phase A's SELECT
// and its per-row UPDATE. Production builds never assign it; the
// `//go:build cgo && test` variant in testing.go writes the real dispatcher.
var reencryptPhaseHook func(ctx context.Context)
