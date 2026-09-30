// Package backup provides online SQLite backup and key snapshot utilities.
package backup

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"time"

	"credential_router/internal/credmgr/crypto"
)

// KeySnapshot binary layout (122 bytes with current sizes):
//
//	[0..4)     magic        4 bytes  "KSNP"
//	[4]        format_ver   1 byte
//	[5]        crypto_mode  1 byte
//	[6..14)    taken_at     8 bytes  little-endian uint64
//	[14..46)   S1 shard     32 bytes
//	[46..78)   S2 shard     32 bytes
//	[78..122)  wrapped DEK  44 bytes
//
// Total = KeySnapshotSize, derived from the components below so changing
// crypto.ShardSize or crypto.WrappedDEKSize (and only those two) shifts
// every dependent offset and the total in lockstep.
const (
	KeySnapshotMagic       = "KSNP"
	KeySnapshotFormatVer   = 0x01
	KeySnapshotMagicOffset = 0
	KeySnapshotMagicSize   = 4

	KeySnapshotHeaderSize = KeySnapshotMagicSize + 1 + 1 + 8
	KeySnapshotS1Offset   = KeySnapshotHeaderSize
	KeySnapshotS2Offset   = KeySnapshotS1Offset + crypto.ShardSize
	KeySnapshotDEKOffset  = KeySnapshotS2Offset + crypto.ShardSize

	KeySnapshotSize = KeySnapshotDEKOffset + crypto.WrappedDEKSize

	KeySnapshotModeAES = 0x01
	KeySnapshotModeSM  = 0x02
)

// KeySnapshot is the in-memory representation of a key snapshot file.
type KeySnapshot struct {
	FormatVersion byte
	CryptoMode    byte // 0x01=AES, 0x02=SM
	TakenAtUnix   uint64
	S1            [crypto.ShardSize]byte
	S2            [crypto.ShardSize]byte
	WrappedDEK    []byte // crypto.WrappedDEKSize bytes
}

// ReadKeySnapshot reads a key snapshot file from disk.
func ReadKeySnapshot(path string) (*KeySnapshot, error) {
	n, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("key_snapshot: read %s: %w", path, err)
	}
	if len(n) != KeySnapshotSize {
		return nil, fmt.Errorf("%w: %s has %d bytes, want %d", ErrKeySnapshotSize, path, len(n), KeySnapshotSize)
	}

	// Validate magic
	if string(n[0:4]) != KeySnapshotMagic {
		return nil, fmt.Errorf("%w: %s has bad magic %q", ErrKeySnapshotMagic, path, string(n[0:4]))
	}

	snap := &KeySnapshot{
		FormatVersion: n[4],
		CryptoMode:    n[5],
		TakenAtUnix:   binary.LittleEndian.Uint64(n[6:14]),
		WrappedDEK:    make([]byte, crypto.WrappedDEKSize),
	}
	if snap.FormatVersion != KeySnapshotFormatVer {
		return nil, fmt.Errorf("key_snapshot: unsupported format_version 0x%02x", snap.FormatVersion)
	}
	if snap.CryptoMode != KeySnapshotModeAES && snap.CryptoMode != KeySnapshotModeSM {
		return nil, fmt.Errorf("key_snapshot: unknown crypto_mode 0x%02x", snap.CryptoMode)
	}
	copy(snap.S1[:], n[KeySnapshotS1Offset:KeySnapshotS2Offset])
	copy(snap.S2[:], n[KeySnapshotS2Offset:KeySnapshotDEKOffset])
	copy(snap.WrappedDEK, n[KeySnapshotDEKOffset:KeySnapshotSize])
	return snap, nil
}

// WriteKeySnapshot writes a key snapshot file. Mode 0600 is POSIX-only
// (os.WriteFile ignores it on Windows; the file inherits parent NTFS ACL).
func WriteKeySnapshot(path string, snap *KeySnapshot) error {
	if snap == nil {
		return fmt.Errorf("key_snapshot: nil snapshot")
	}
	if len(snap.WrappedDEK) != crypto.WrappedDEKSize {
		return fmt.Errorf("key_snapshot: wrapped_dek length %d, want %d", len(snap.WrappedDEK), crypto.WrappedDEKSize)
	}
	data := make([]byte, KeySnapshotSize)
	copy(data[0:4], KeySnapshotMagic)
	data[4] = snap.FormatVersion
	data[5] = snap.CryptoMode
	binary.LittleEndian.PutUint64(data[6:14], snap.TakenAtUnix)
	copy(data[KeySnapshotS1Offset:KeySnapshotS2Offset], snap.S1[:])
	copy(data[KeySnapshotS2Offset:KeySnapshotDEKOffset], snap.S2[:])
	copy(data[KeySnapshotDEKOffset:KeySnapshotSize], snap.WrappedDEK)

	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("key_snapshot: write %s: %w", path, err)
	}
	return nil
}

// NewKeySnapshot creates a snapshot with current timestamp and format_version=0x01.
func NewKeySnapshot(mode byte, s1, s2 [crypto.ShardSize]byte, wrappedDEK []byte) *KeySnapshot {
	return &KeySnapshot{
		FormatVersion: KeySnapshotFormatVer,
		CryptoMode:    mode,
		TakenAtUnix:   uint64(time.Now().Unix()),
		S1:            s1,
		S2:            s2,
		WrappedDEK:    wrappedDEK,
	}
}

var ErrKeySnapshotSize = errors.New("key_snapshot: invalid file size")
var ErrKeySnapshotMagic = errors.New("key_snapshot: bad magic")
