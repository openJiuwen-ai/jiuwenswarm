package keystore

import (
	"fmt"
	"os"
	"runtime"

	"credential_router/internal/credmgr/crypto"
)

const (
	// ShardSize is re-exported from crypto to break the backup→keystore
	// import cycle; the authoritative definition is crypto.ShardSize.
	ShardSize = crypto.ShardSize
	// KeyFileMode is the POSIX mode used when writing key files. On
	// Unix it restricts access to the owner; on Windows os.WriteFile
	// ignores the mode and the file inherits the parent NTFS ACL.
	KeyFileMode = 0o600
)

// S3Shard is the third shard in the KEK derivation, hardcoded into the
// binary. The 32-byte value is "OwnerShard3KeyMaterial2026-SM3-F" (visible
// ASCII prefix + SM3 fingerprint) — domain separation only, not a secret.
// S3 is never rotated: rotating it would require redistributing the binary.
var S3Shard = [ShardSize]byte{
	0x4f, 0x77, 0x6e, 0x65, 0x72, 0x53, 0x68, 0x61,
	0x72, 0x64, 0x33, 0x4b, 0x65, 0x79, 0x4d, 0x61,
	0x74, 0x65, 0x72, 0x69, 0x61, 0x6c, 0x32, 0x30,
	0x32, 0x36, 0x2d, 0x53, 0x4d, 0x33, 0x2d, 0x46,
}

// LoadS1FromFile reads a 32-byte S1 shard from a 0600 file. Returns
// ErrShardLength if file is wrong size. Startup-blocking errors
// (stat/open/read/perm) wrap ErrStartupRefused. The 0600 mode check is
// skipped on Windows because os.Stat mode bits don't carry POSIX
// semantics there; NTFS ACL is the access boundary.
func LoadS1FromFile(path string) ([ShardSize]byte, error) {
	var shard [ShardSize]byte
	info, err := os.Stat(path)
	if err != nil {
		return shard, fmt.Errorf("%w: stat S1 file: %v", ErrStartupRefused, err)
	}
	if info.Size() != ShardSize {
		return shard, fmt.Errorf("%w: S1 file %s has %d bytes, want %d", ErrShardLength, path, info.Size(), ShardSize)
	}
	if mode := info.Mode().Perm(); mode != KeyFileMode && runtime.GOOS != "windows" {
		return shard, fmt.Errorf("%w: S1 file %s has mode %o, want %o", ErrStartupRefused, path, mode, KeyFileMode)
	}
	f, err := os.Open(path)
	if err != nil {
		return shard, fmt.Errorf("%w: open S1 file: %v", ErrStartupRefused, err)
	}
	defer f.Close()
	n, err := f.Read(shard[:])
	if err != nil {
		return shard, fmt.Errorf("%w: read S1 file: %v", ErrStartupRefused, err)
	}
	if n != ShardSize {
		return shard, fmt.Errorf("%w: short read %d bytes", ErrShardLength, n)
	}
	return shard, nil
}

// WriteS1ToFile writes a 32-byte S1 shard to a file with mode 0600
// (POSIX-only; on Windows the file inherits parent NTFS ACL). Refuses
// to write if the shard is all zeros (defense against accidental use
// of uninitialized key).
func WriteS1ToFile(path string, shard [ShardSize]byte) error {
	allZero := true
	for _, b := range shard {
		if b != 0 {
			allZero = false
			break
		}
	}
	if allZero {
		return fmt.Errorf("keystore: refusing to write all-zero S1 shard to %s", path)
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, KeyFileMode)
	if err != nil {
		return fmt.Errorf("keystore: create S1 file: %w", err)
	}
	defer f.Close()
	n, err := f.Write(shard[:])
	if err != nil {
		return fmt.Errorf("keystore: write S1 file: %w", err)
	}
	if n != ShardSize {
		return shard_too_short_err(n)
	}
	// Ensure 0600 even if file existed
	if err := os.Chmod(path, KeyFileMode); err != nil {
		return fmt.Errorf("keystore: chmod S1 file: %w", err)
	}
	return nil
}

func shard_too_short_err(n int) error {
	return fmt.Errorf("%w: short write %d bytes", ErrShardLength, n)
}

// XorThree derives a 32-byte intermediate by XORing three 32-byte shards.
// Used to combine S1 + S2 + S3 for KEK derivation.
func XorThree(s1, s2, s3 [ShardSize]byte) [ShardSize]byte {
	var out [ShardSize]byte
	for i := 0; i < ShardSize; i++ {
		out[i] = s1[i] ^ s2[i] ^ s3[i]
	}
	return out
}
