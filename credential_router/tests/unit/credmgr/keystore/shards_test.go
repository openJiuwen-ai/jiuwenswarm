package keystore_test

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"credential_router/internal/credmgr/keystore"
)

func TestS3ShardLength(t *testing.T) {
	if len(keystore.S3Shard) != keystore.ShardSize {
		t.Errorf("S3Shard length = %d, want %d", len(keystore.S3Shard), keystore.ShardSize)
	}
	allZero := true
	for _, b := range keystore.S3Shard {
		if b != 0 {
			allZero = false
			break
		}
	}
	if allZero {
		t.Error("S3Shard is all zeros (invalid)")
	}
}

func TestS1FileRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s1.bin")

	orig := [keystore.ShardSize]byte{}
	for i := range orig {
		orig[i] = byte(i + 1) // non-zero
	}

	if err := keystore.WriteS1ToFile(path, orig); err != nil {
		t.Fatalf("Write: %v", err)
	}

	// Verify file mode
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	// Windows tracks file access via ACLs rather than Unix mode bits, so Go's
	// os.Stat does not surface KeyFileMode there. The production code's mode
	 // is a benign no-op on Windows filesystems; this assertion only validates
	// the POSIX path.
	if runtime.GOOS != "windows" && info.Mode().Perm() != keystore.KeyFileMode {
		t.Errorf("mode = %o, want %o", info.Mode().Perm(), keystore.KeyFileMode)
	}

	got, err := keystore.LoadS1FromFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != orig {
		t.Error("round-trip mismatch")
	}
}

func TestS1FileRefuseAllZero(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s1.bin")
	var zero [keystore.ShardSize]byte
	err := keystore.WriteS1ToFile(path, zero)
	if err == nil {
		t.Error("expected error for all-zero shard")
	}
}

func TestS1FileWrongLength(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s1.bin")

	if err := os.WriteFile(path, make([]byte, 16), keystore.KeyFileMode); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.LoadS1FromFile(path)
	if err == nil {
		t.Error("expected error for wrong-length file")
	}
}

func TestS1FileMissing(t *testing.T) {
	_, err := keystore.LoadS1FromFile("/nonexistent/path/s1.bin")
	if err == nil {
		t.Error("expected error for missing file")
	}
}

func TestS1FileBadMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s1.bin")

	shard := [keystore.ShardSize]byte{1, 2, 3}
	if err := os.WriteFile(path, shard[:], 0o644); err != nil { // wrong mode
		t.Fatal(err)
	}
	_, err := keystore.LoadS1FromFile(path)
	if err == nil {
		t.Error("expected error for wrong mode")
	}
}

func TestXorThree(t *testing.T) {
	a := [keystore.ShardSize]byte{1, 2, 3}
	b := [keystore.ShardSize]byte{4, 5, 6}
	c := [keystore.ShardSize]byte{7, 8, 9}
	result := keystore.XorThree(a, b, c)
	expected := [keystore.ShardSize]byte{1 ^ 4 ^ 7, 2 ^ 5 ^ 8, 3 ^ 6 ^ 9}
	if result != expected {
		t.Errorf("XorThree = %v, want %v", result, expected)
	}
}

func TestXorThreeWithZeros(t *testing.T) {
	a := [keystore.ShardSize]byte{0xFF}
	var zero [keystore.ShardSize]byte
	// a ^ 0 ^ 0 = a
	if got := keystore.XorThree(a, zero, zero); got != a {
		t.Error("XorThree with zero shards should be identity")
	}
	// a ^ a ^ a = a (since a ^ a = 0, and 0 ^ a = a)
	if got := keystore.XorThree(a, a, a); got != a {
		t.Error("XorThree of same shard 3 times should be a (since a^a^a = a)")
	}
}

func TestErrShardLengthSentinel(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s1.bin")
	if err := os.WriteFile(path, make([]byte, 16), keystore.KeyFileMode); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.LoadS1FromFile(path)
	if err == nil {
		t.Fatal("expected error")
	}
	if !errors.Is(err, keystore.ErrShardLength) {
		t.Errorf("got %v, want ErrShardLength", err)
	}
}
