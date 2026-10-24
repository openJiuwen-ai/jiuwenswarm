//go:build cgo && test

package backup

import "credential_router/internal/credmgr/store"

// Test-support accessors for the external white-box test suite in
// tests/unit/backup. The `cgo && test` build constraint excludes this file
// from production builds (both non-cgo and cgo `go build ./...`) while
// leaving the symbols available during `go test ./tests/unit/...`.

// StoreForTesting returns the store reference captured by NewBackupManager.
// The manager's store wiring is otherwise only observable through Backup(),
// which needs a live database, so the nil-passthrough contract of the
// constructor is not reachable through any other public method.
func (bm *BackupManager) StoreForTesting() *store.Store { return bm.store }

// DeleteOldestForTesting runs retention deletion against an arbitrary glob and
// keep count. Retention selection has no other public entry point: Backup and
// ScanRetention hard-wire the globs and keep counts from a BackupConfig, so
// arbitrary globs/policies (including keep == 0) can only be exercised
// through this hook.
func DeleteOldestForTesting(glob string, keep int) error { return deleteOldest(glob, keep) }

// TplFilenameForTesting expands a filename template, substituting {type} and
// {ts}. Template expansion is otherwise embedded in Backup(), which chooses
// its own timestamp, so arbitrary templates (and the {ts}/{type} substitution
// contract) are not reachable through the public API.
func TplFilenameForTesting(tpl, btype string, ts int64) string { return tplFilename(tpl, btype, ts) }

// TplGlobForTesting derives the retention glob from a filename template.
// Test-only for the same reason as TplFilenameForTesting: the derivation is
// otherwise only reachable through the hard-wired retention sweep.
func TplGlobForTesting(tpl, btype string) string { return tplGlob(tpl, btype) }
