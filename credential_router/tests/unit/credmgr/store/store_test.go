//go:build cgo

package store_test

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"credential_router/internal/credmgr/store"
)

func ptr[T any](v T) *T { return &v }

// ---------------------------------------------------------------------------
// 1. TestOpenInMemory — Open(":memory:") succeeds
// ---------------------------------------------------------------------------
func TestOpenInMemory(t *testing.T) {
	s, err := store.OpenForTesting(":memory:")
	if err != nil {
		t.Fatalf("Open(':memory:') failed: %v", err)
	}
	defer s.Close()
	if s.AdminDB() == nil {
		t.Fatal("DB() returned nil")
	}
}

// ---------------------------------------------------------------------------
// 2. TestSchemaCreated — tables exist
// ---------------------------------------------------------------------------
func TestSchemaCreated(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	tables := []string{"credentials", "key_metadata"}
	for _, tbl := range tables {
		if !tableExists(t, s.AdminDB(), tbl) {
			t.Errorf("table %q does not exist", tbl)
		}
	}
}

// ---------------------------------------------------------------------------
// 4. TestClose — Close works, Ping fails after
// ---------------------------------------------------------------------------
func TestClose(t *testing.T) {
	s, err := store.OpenForTesting(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close failed: %v", err)
	}
	if err := s.AdminDB().Ping(); err == nil {
		t.Fatal("expected Ping to fail after Close")
	}
}

// ---------------------------------------------------------------------------
// 5. TestPragmasApplied — foreign_keys = 1
// ---------------------------------------------------------------------------
func TestPragmasApplied(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	var fk int
	if err := s.AdminDB().QueryRow("PRAGMA foreign_keys").Scan(&fk); err != nil {
		t.Fatalf("query PRAGMA foreign_keys: %v", err)
	}
	if fk != 1 {
		t.Fatalf("expected foreign_keys=1, got %d", fk)
	}
}

// ---------------------------------------------------------------------------
// 6. TestOpenInvalidPath — nonexistent dir fails
// ---------------------------------------------------------------------------
func TestOpenInvalidPath(t *testing.T) {
	_, err := store.OpenForTesting("/nonexistent_dir_xyz/db.sqlite")
	if err == nil {
		t.Fatal("expected error for invalid path")
	}
}

// ---------------------------------------------------------------------------
// 7. TestReopenSamePath — schema persists across open/close
// ---------------------------------------------------------------------------
func TestReopenSamePath(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")
	s1, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("first Open: %v", err)
	}
	if err := s1.Close(); err != nil {
		t.Fatalf("first Close: %v", err)
	}
	s2, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("second Open: %v", err)
	}
	defer s2.Close()
	if !tableExists(t, s2.AdminDB(), "credentials") || !tableExists(t, s2.AdminDB(), "key_metadata") {
		t.Fatal("tables missing after reopen")
	}
}

// ---------------------------------------------------------------------------
// 8. TestUniqueConstraint — duplicate (user_id, api_base, key_tag) fails
// ---------------------------------------------------------------------------
func TestUniqueConstraint(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	insert := `INSERT INTO credentials
		(user_id, api_base, key_tag, proxy_key, api_key_cipher, auth_type, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
	_, err := s.AdminDB().Exec(insert, "u1", "https://example.com", "default",
		"cr_pk_0000000000000000000000000000000000000000001", []byte("cipher"), "openai", 1000, 1000)
	if err != nil {
		t.Fatalf("first insert: %v", err)
	}
	_, err = s.AdminDB().Exec(insert, "u1", "https://example.com", "default",
		"cr_pk_0000000000000000000000000000000000000000002", []byte("cipher2"), "anthropic", 2000, 2000)
	if err == nil {
		t.Fatal("expected unique constraint violation")
	}
}

// ---------------------------------------------------------------------------
// 9. TestIndexExists — idx_credentials_urt exists
// ---------------------------------------------------------------------------
func TestIndexExists(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	var name string
	err := s.AdminDB().QueryRow(
		`SELECT name FROM sqlite_master WHERE type='index' AND name='idx_credentials_urt'`,
	).Scan(&name)
	if err != nil {
		t.Fatalf("index idx_credentials_urt not found: %v", err)
	}
}

// ---------------------------------------------------------------------------
// 10. TestColumnsExist — verify all columns from spec
// ---------------------------------------------------------------------------
func TestColumnsExist(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	expectedCredentials := []string{
		"id", "user_id", "api_base", "key_tag", "api_key_cipher",
		"auth_type", "row_version", "kek_version", "dek_version",
		"created_at", "updated_at",
	}
	expectedKeyMeta := []string{
		"id", "active_kek_version", "pending_kek_version",
		"active_dek_version", "pending_dek_version", "crypto_mode",
		"active_config_shard", "pending_config_shard", "file_shard_version",
		"file_shard_rotated_at", "last_rotate_at", "wrapped_dek",
		"pending_wrapped_dek", "dek_rotated_at", "updated_at",
	}
	for _, col := range expectedCredentials {
		if !columnExists(t, s.AdminDB(), "credentials", col) {
			t.Errorf("column %q not found in credentials", col)
		}
	}
	for _, col := range expectedKeyMeta {
		if !columnExists(t, s.AdminDB(), "key_metadata", col) {
			t.Errorf("column %q not found in key_metadata", col)
		}
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
func openMem(t *testing.T) *store.Store {
	t.Helper()
	s, err := store.OpenForTesting(":memory:")
	if err != nil {
		t.Fatalf("Open(':memory:'): %v", err)
	}
	return s
}

func tableExists(t *testing.T, db *sql.DB, name string) bool {
	t.Helper()
	var n int
	err := db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?`, name,
	).Scan(&n)
	if err != nil {
		t.Fatalf("check table %q: %v", name, err)
	}
	return n > 0
}

func columnExists(t *testing.T, db *sql.DB, table, column string) bool {
	t.Helper()
	rows, err := db.Query(`PRAGMA table_info(` + table + `)`)
	if err != nil {
		t.Fatalf("PRAGMA table_info %q: %v", table, err)
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull int
		var dflt sql.NullString
		var pk int
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			t.Fatalf("scan table_info row: %v", err)
		}
		if name == column {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// 11. TestOpenWithConfigCustomConns — admin/proxy pool sizes honor the config
// ---------------------------------------------------------------------------
func TestOpenWithConfigCustomConns(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")

	seed, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("seed store.OpenForTesting: %v", err)
	}
	if err := seed.Close(); err != nil {
		t.Fatalf("seed Close: %v", err)
	}

	s, err := store.OpenWithConfig(store.OpenConfig{
		Path:          dbPath,
		AdminMaxConns: 1,
		ProxyMaxConns: 4,
	})
	if err != nil {
		t.Fatalf("store.OpenWithConfig: %v", err)
	}
	defer s.Close()

	if got := s.AdminDB().Stats().MaxOpenConnections; got != 1 {
		t.Errorf("admin MaxOpenConnections = %d, want 1", got)
	}
	if got := s.ProxyDB().Stats().MaxOpenConnections; got != 4 {
		t.Errorf("proxy MaxOpenConnections = %d, want 4", got)
	}

	sDef, err := store.OpenWithConfig(store.OpenConfig{Path: dbPath})
	if err != nil {
		t.Fatalf("store.OpenWithConfig defaults: %v", err)
	}
	if got := sDef.AdminDB().Stats().MaxOpenConnections; got != store.DefaultAdminMaxConns {
		t.Errorf("default admin MaxOpenConnections = %d, want %d", got, store.DefaultAdminMaxConns)
	}
	if got := sDef.ProxyDB().Stats().MaxOpenConnections; got != store.DefaultProxyMaxConns {
		t.Errorf("default proxy MaxOpenConnections = %d, want %d", got, store.DefaultProxyMaxConns)
	}
	sDef.Close()

	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if err := s.AdminDB().Ping(); err == nil {
		t.Error("AdminDB ping succeeded after Close")
	}
	if err := s.ProxyDB().Ping(); err == nil {
		t.Error("ProxyDB ping succeeded after Close")
	}
}

// ---------------------------------------------------------------------------
//  12. TestProxyReadsDuringAdminWriteTx — WAL: proxy reads stay live while an
//     admin write transaction is open
//
// ---------------------------------------------------------------------------
func TestProxyReadsDuringAdminWriteTx(t *testing.T) {
	s := openTempStore(t)
	defer s.Close()
	ctx := context.Background()

	orig := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctx, orig); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	tx, err := s.AdminDB().BeginTx(ctx, nil)
	if err != nil {
		t.Fatalf("BeginTx: %v", err)
	}
	defer tx.Rollback()
	now := time.Now().Unix()
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO credentials (user_id, api_base, key_tag, proxy_key, api_key_cipher, auth_type, row_version, kek_version, dek_version, created_at, updated_at)
		 VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?)`,
		"u2", "https://example.com", "default", "cr_pk_pendingtx000000000000000000000000000000000000000", []byte("pending-cipher"), "openai", now, now); err != nil {
		t.Fatalf("tx insert: %v", err)
	}

	results := make(chan error, 5)
	for i := 0; i < 5; i++ {
		go func() {
			rctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			got, err := s.GetCredentialByUserURLTag(rctx, "u1", "https://example.com", "default")
			if err != nil {
				results <- err
				return
			}
			if got.ID != orig.ID {
				results <- fmt.Errorf("read ID = %d, want %d", got.ID, orig.ID)
				return
			}
			results <- nil
		}()
	}
	for i := 0; i < 5; i++ {
		if err := <-results; err != nil {
			t.Fatalf("proxy read %d blocked by open admin write tx: %v", i, err)
		}
	}

	if err := tx.Commit(); err != nil {
		t.Fatalf("tx commit: %v", err)
	}
}
