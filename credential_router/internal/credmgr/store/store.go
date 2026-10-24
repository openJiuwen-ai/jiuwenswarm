//go:build cgo

// Package store provides the SQLite persistence layer for credential_router.
//
// Open() auto-bootstraps the schema (see credentials_schema.go) on first connection, so
// callers do not need a separate migrate step. There is no installed base to
// remain compatible with — the schema is in-tree.
//
// Connection pool split (Q3/Q4):
//   - adminDB: MaxOpenConns(1) by default, used for writes + rotation txns
//   - proxyDB:  MaxOpenConns(N) configurable, used for proxy hot reads
//
// Both *sql.DB point at the same SQLite file; WAL mode allows the reader
// pool to run concurrently with the single writer.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	_ "github.com/mattn/go-sqlite3"
)

// ErrPhaseADecryptFail is returned by ReencryptDekVersionBatch when a row's
// ciphertext cannot be decrypted with the active snapshot's DEK. This
// indicates either data corruption on disk or a DEK that's been rolled
// forward past this row's ciphertext version. Callers MUST treat this as
// a fatal condition: log the error and exit with status 78
// (sysexits.h EX_CONFIG). Neither cause is recoverable in-process.
var ErrPhaseADecryptFail = errors.New("store: Phase A decrypt failed — data corruption")

const (
	DefaultAdminMaxConns = 2
	DefaultProxyMaxConns = 32
)

// OpenConfig configures a Store. Zero values use defaults.
type OpenConfig struct {
	Path          string
	AdminMaxConns int // 0 → 2
	ProxyMaxConns int // 0 → 32
}

// Store encapsulates a SQLite database connection split across two pools:
// adminDB for writes + rotation transactions, proxyDB for proxy hot reads.
type Store struct {
	adminDB *sql.DB
	proxyDB *sql.DB
	path    string
}

// Open opens (or creates) a SQLite database at path with default pool sizes
// (admin=2, proxy=32) and auto-bootstraps the schema. Use ":memory:" for an
// in-memory database.
func Open(path string) (*Store, error) {
	return OpenWithConfig(OpenConfig{Path: path})
}

// OpenWithConfig is the configurable constructor. See OpenConfig for defaults.
func OpenWithConfig(cfg OpenConfig) (*Store, error) {
	s, err := openRaw(cfg)
	if err != nil {
		return nil, err
	}
	if err := bootstrapSchema(s.adminDB); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// OpenForTesting is a convenience wrapper around OpenWithConfig for tests.
func OpenForTesting(path string) (*Store, error) {
	return OpenWithConfig(OpenConfig{Path: path})
}

func openRaw(cfg OpenConfig) (*Store, error) {
	path := cfg.Path
	adminMax := cfg.AdminMaxConns
	if adminMax <= 0 {
		adminMax = DefaultAdminMaxConns
	}
	proxyMax := cfg.ProxyMaxConns
	if proxyMax <= 0 {
		proxyMax = DefaultProxyMaxConns
	}

	dsn := path
	if path == ":memory:" {
		dsn = "file::memory:?cache=shared&_busy_timeout=5000"
	} else {
		dsn = path + "?_busy_timeout=5000"
	}

	adminDB, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("store: sql.Open adminDB: %w", err)
	}
	adminDB.SetMaxOpenConns(adminMax)
	if err := adminDB.PingContext(context.Background()); err != nil {
		_ = adminDB.Close()
		return nil, fmt.Errorf("store: ping adminDB: %w", err)
	}

	proxyDB, err := sql.Open("sqlite3", dsn)
	if err != nil {
		_ = adminDB.Close()
		return nil, fmt.Errorf("store: sql.Open proxyDB: %w", err)
	}
	proxyDB.SetMaxOpenConns(proxyMax)
	if err := proxyDB.PingContext(context.Background()); err != nil {
		_ = proxyDB.Close()
		_ = adminDB.Close()
		return nil, fmt.Errorf("store: ping proxyDB: %w", err)
	}

	s := &Store{
		adminDB: adminDB,
		proxyDB: proxyDB,
		path:    path,
	}
	if err := s.applyPragmas(path != ":memory:"); err != nil {
		_ = proxyDB.Close()
		_ = adminDB.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) applyPragmas(wal bool) error {
	if wal {
		if _, err := s.adminDB.Exec(`PRAGMA journal_mode=WAL`); err != nil {
			return fmt.Errorf("store: journal_mode=WAL: %w", err)
		}
	}
	if _, err := s.adminDB.Exec(`PRAGMA synchronous=NORMAL`); err != nil {
		return fmt.Errorf("store: synchronous=NORMAL: %w", err)
	}
	if _, err := s.adminDB.Exec(`PRAGMA foreign_keys=ON`); err != nil {
		return fmt.Errorf("store: foreign_keys=ON: %w", err)
	}
	return nil
}

// Close closes both connection pools. Safe to call multiple times.
func (s *Store) Close() error {
	var firstErr error
	if s.adminDB != nil {
		if err := s.adminDB.Close(); err != nil {
			firstErr = err
		}
	}
	if s.proxyDB != nil {
		if err := s.proxyDB.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// AdminDB returns the write-path *sql.DB (MaxOpenConns typically 1). Intended
// for migrate CLI and tests that need raw access for setup/verification.
func (s *Store) AdminDB() *sql.DB {
	return s.adminDB
}

// ProxyDB returns the read-path *sql.DB (MaxOpenConns typically >1) for proxy
// hot reads. Tests may also use this.
func (s *Store) ProxyDB() *sql.DB {
	return s.proxyDB
}



// Path returns the database path passed to Open.
func (s *Store) Path() string {
	return s.path
}
