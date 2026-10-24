package backup_test

import (
	"bytes"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"credential_router/internal/credmgr/backup"
)

func TestKeySnapshotRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	var s1, s2 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	for i := range s2 {
		s2[i] = byte(i + 100)
	}
	wrapped := bytes.Repeat([]byte{0xAB}, 44)

	snap := backup.NewKeySnapshot(backup.KeySnapshotModeAES, s1, s2, wrapped)
	if err := backup.WriteKeySnapshot(path, snap); err != nil {
		t.Fatal(err)
	}

	got, err := backup.ReadKeySnapshot(path)
	if err != nil {
		t.Fatal(err)
	}

	if got.FormatVersion != snap.FormatVersion {
		t.Errorf("FormatVersion: got 0x%02x, want 0x%02x", got.FormatVersion, snap.FormatVersion)
	}
	if got.CryptoMode != snap.CryptoMode {
		t.Errorf("CryptoMode: got 0x%02x, want 0x%02x", got.CryptoMode, snap.CryptoMode)
	}
	if got.TakenAtUnix != snap.TakenAtUnix {
		t.Errorf("TakenAtUnix: got %d, want %d", got.TakenAtUnix, snap.TakenAtUnix)
	}
	if got.S1 != s1 {
		t.Error("S1 mismatch")
	}
	if got.S2 != s2 {
		t.Error("S2 mismatch")
	}
	if !bytes.Equal(got.WrappedDEK, wrapped) {
		t.Error("wrapped_dek mismatch")
	}
}

func TestKeySnapshotFileMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	var s1, s2 [32]byte
	snap := backup.NewKeySnapshot(backup.KeySnapshotModeAES, s1, s2, make([]byte, 44))
	if err := backup.WriteKeySnapshot(path, snap); err != nil {
		t.Fatal(err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	// Windows tracks file access via ACLs rather than Unix mode bits, so Go's
	// os.Stat does not surface 0o600 there. The production code's 0o600 mode
	// is a benign no-op on Windows filesystems; this assertion only validates
	// the POSIX path.
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Errorf("file mode = %o, want 0o600", info.Mode().Perm())
	}
}

func TestKeySnapshotExactSize(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	var s1, s2 [32]byte
	snap := backup.NewKeySnapshot(backup.KeySnapshotModeAES, s1, s2, make([]byte, 44))
	if err := backup.WriteKeySnapshot(path, snap); err != nil {
		t.Fatal(err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != backup.KeySnapshotSize {
		t.Errorf("size = %d, want %d", info.Size(), backup.KeySnapshotSize)
	}
}

func TestKeySnapshotBadMagic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	data := make([]byte, backup.KeySnapshotSize)
	copy(data[0:4], "BAD!")
	data[4] = backup.KeySnapshotFormatVer
	data[5] = backup.KeySnapshotModeAES
	binary.LittleEndian.PutUint64(data[6:14], 1234567890)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := backup.ReadKeySnapshot(path)
	if err == nil {
		t.Error("expected error for bad magic")
	}
}

func TestKeySnapshotWrongSize(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	data := make([]byte, 100)
	copy(data[0:4], backup.KeySnapshotMagic)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := backup.ReadKeySnapshot(path)
	if err == nil {
		t.Error("expected error for wrong size")
	}
	if !errors.Is(err, backup.ErrKeySnapshotSize) {
		t.Errorf("got %v, want backup.ErrKeySnapshotSize", err)
	}
}

func TestKeySnapshotUnknownMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	data := make([]byte, backup.KeySnapshotSize)
	copy(data[0:4], backup.KeySnapshotMagic)
	data[4] = backup.KeySnapshotFormatVer
	data[5] = 0x99
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := backup.ReadKeySnapshot(path)
	if err == nil {
		t.Error("expected error for unknown mode")
	}
}

func TestKeySnapshotUnknownFormat(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "key_snapshot.bin")

	data := make([]byte, backup.KeySnapshotSize)
	copy(data[0:4], backup.KeySnapshotMagic)
	data[4] = 0xFF
	data[5] = backup.KeySnapshotModeAES
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}

	_, err := backup.ReadKeySnapshot(path)
	if err == nil {
		t.Error("expected error for unknown format")
	}
}

func TestKeySnapshotWriteNil(t *testing.T) {
	if err := backup.WriteKeySnapshot(filepath.Join(t.TempDir(), "x"), nil); err == nil {
		t.Error("expected error for nil snapshot")
	}
}

func TestKeySnapshotWriteBadWrappedLen(t *testing.T) {
	snap := backup.NewKeySnapshot(backup.KeySnapshotModeAES, [32]byte{}, [32]byte{}, make([]byte, 30))
	if err := backup.WriteKeySnapshot(filepath.Join(t.TempDir(), "x"), snap); err == nil {
		t.Error("expected error for bad wrapped_dek length")
	}
}
