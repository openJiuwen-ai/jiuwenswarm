//go:build cgo

// Package backup provides online SQLite backup using sqlite3's built-in backup API.
//
// Unlike file-level copies, this API produces a consistent snapshot even while
// the source database is being written to.  It is safe to call while the store
// is serving live traffic.
package backup

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	sqlite3 "github.com/mattn/go-sqlite3"
)

// Backup creates a consistent online snapshot of srcDB into destPath.
//
// It uses sqlite3's online backup API (sqlite3_backup_init / sqlite3_backup_step)
// via mattn/go-sqlite3's SQLiteConn.Backup method.  The backup is safe to run
// while the source database is being written to — no file-level locking issues.
//
// The destination file and any missing parent directories are created implicitly.
func Backup(srcDB *sql.DB, destPath string) error {
	if srcDB == nil {
		return fmt.Errorf("backup: nil source DB")
	}
	if destPath == "" {
		return fmt.Errorf("backup: empty dest path")
	}

	// Ensure parent directory exists.
	if err := os.MkdirAll(filepath.Dir(destPath), 0o755); err != nil {
		return fmt.Errorf("backup: create dest dir: %w", err)
	}

	// Open the destination database.
	destDB, err := sql.Open("sqlite3", destPath)
	if err != nil {
		return fmt.Errorf("backup: open dest: %w", err)
	}
	defer destDB.Close()

	// Grab source connection.
	srcConn, err := srcDB.Conn(context.Background())
	if err != nil {
		return fmt.Errorf("backup: get src conn: %w", err)
	}
	defer srcConn.Close()

	// Grab destination connection.
	destConn, err := destDB.Conn(context.Background())
	if err != nil {
		return fmt.Errorf("backup: get dest conn: %w", err)
	}
	defer destConn.Close()

	// Obtain the raw *sqlite3.SQLiteConn for the source.
	var srcS3Conn *sqlite3.SQLiteConn
	if err := srcConn.Raw(func(driverConn any) error {
		var ok bool
		srcS3Conn, ok = driverConn.(*sqlite3.SQLiteConn)
		if !ok {
			return fmt.Errorf("backup: src conn is not *sqlite3.SQLiteConn (got %T)", driverConn)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("backup: src raw: %w", err)
	}

	// Perform the backup on the destination connection.
	if err := destConn.Raw(func(driverConn any) error {
		destS3Conn, ok := driverConn.(*sqlite3.SQLiteConn)
		if !ok {
			return fmt.Errorf("backup: dest conn is not *sqlite3.SQLiteConn (got %T)", driverConn)
		}

		backup, err := destS3Conn.Backup("main", srcS3Conn, "main")
		if err != nil {
			return fmt.Errorf("backup: init: %w", err)
		}

		// Copy all remaining pages in a single step.
		done, err := backup.Step(-1)
		if err != nil {
			_ = backup.Finish()
			return fmt.Errorf("backup: step: %w", err)
		}
		if !done {
			_ = backup.Finish()
			return fmt.Errorf("backup: step returned done=false after copying all pages")
		}

		if err := backup.Finish(); err != nil {
			return fmt.Errorf("backup: finish: %w", err)
		}
		return nil
	}); err != nil {
		return fmt.Errorf("backup: dest raw: %w", err)
	}

	return nil
}
