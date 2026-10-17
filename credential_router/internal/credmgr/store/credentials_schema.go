//go:build cgo

package store

import (
	"database/sql"
	"fmt"
)

// bootstrapSchema creates the credential_router schema if it does not already
// exist. Idempotent: every statement uses IF NOT EXISTS. The schema lives
// in-tree and is created on first connection; there is no separate migrate
// command.
//
// Future schema changes are made by editing this function and bumping the
// version recorded in CHANGELOG.
func bootstrapSchema(db *sql.DB) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS credentials (
			id              INTEGER PRIMARY KEY AUTOINCREMENT,
			user_id         TEXT    NOT NULL,
			api_base        TEXT    NOT NULL,
			key_tag         TEXT    NOT NULL,
			proxy_key       TEXT    NOT NULL UNIQUE,
			api_key_cipher  BLOB    NOT NULL,
			auth_type       TEXT    NOT NULL,
			row_version     INTEGER NOT NULL DEFAULT 1,
			kek_version     INTEGER NOT NULL DEFAULT 1,
			dek_version     INTEGER NOT NULL DEFAULT 1,
			created_at      INTEGER NOT NULL,
			updated_at      INTEGER NOT NULL,
			UNIQUE (user_id, api_base, key_tag)
		)`,
		`CREATE INDEX IF NOT EXISTS idx_credentials_urt
			ON credentials(user_id, api_base, key_tag)`,
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_credentials_proxy_key
			ON credentials(proxy_key)`,
		`CREATE TABLE IF NOT EXISTS key_metadata (
			id                    INTEGER PRIMARY KEY CHECK (id = 1),
			active_kek_version    INTEGER NOT NULL,
			pending_kek_version   INTEGER NOT NULL DEFAULT 0,
			active_dek_version    INTEGER NOT NULL,
			pending_dek_version   INTEGER NOT NULL DEFAULT 0,
			crypto_mode           TEXT    NOT NULL,
			active_config_shard   BLOB    NOT NULL,
			pending_config_shard  BLOB    NOT NULL DEFAULT x'',
			file_shard_version    INTEGER NOT NULL DEFAULT 1,
			file_shard_rotated_at INTEGER NOT NULL,
			last_rotate_at        INTEGER NOT NULL,
			wrapped_dek           BLOB    NOT NULL,
			pending_wrapped_dek   BLOB    NOT NULL,
			dek_rotated_at        INTEGER NOT NULL DEFAULT 0,
			updated_at            INTEGER NOT NULL
		)`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			return fmt.Errorf("store: bootstrap: %w", err)
		}
	}
	return nil
}
