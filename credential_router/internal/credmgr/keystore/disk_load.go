// Package keystore implements the encrypted credential lifecycle.
//
// File map:
//
//	bootstrap.go            SelfInit: reads pre-placed S1/DEK/crypto_mode,
//	                         generates S2, derives KEK, wraps DEK, inserts
//	                         key_metadata row on first run
//	disk_load.go            (this file) shard persistence reads/writes
//	shard_path.go           shard file path conventions
//	shards.go               S1/S2/S3 origin tracking; KEK derivation (XOR + PBKDF2)
//	snapshot_lifecycle.go   atomic.Pointer[KeySnapshot]; Capture/Release/Swap/WaitInflightDrained
//	rotation.go             KEK + DEK rotation state machine; Phase A loop; StartAutoRotate ticker
//	rotation_recovery.go    startup-time convergence; RecoveryCase enum
//	                         (1=clean, 4=KEK/S1 forward, 5=KEK/S2 forward,
//	                          7=DEK forward, Nested=fatal)
//	errors.go               sentinel error variables
//	testing.go              //go:build test — white-box hooks
package keystore

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/platform"
	"github.com/tjfoc/gmsm/sm3"
)

// ReadCryptoModeFile reads a 1-byte crypto_mode file (0x01=AES, 0x02=SM4).
// The 0600 mode check is POSIX-only; on Windows mode bits don't carry
// POSIX semantics and NTFS ACL governs access.
func ReadCryptoModeFile(path string) (crypto.Mode, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, fmt.Errorf("%w: read crypto_mode file %s: %v", ErrStartupRefused, path, err)
	}
	if mode := info.Mode().Perm(); mode != KeyFileMode && runtime.GOOS != "windows" {
		return 0, fmt.Errorf("%w: crypto_mode file %s has mode %o, want %o", ErrStartupRefused, path, mode, KeyFileMode)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, fmt.Errorf("%w: read crypto_mode file %s: %v", ErrStartupRefused, path, err)
	}
	if len(b) != 1 {
		return 0, fmt.Errorf("%w: crypto_mode file %s has %d bytes, want 1", ErrStartupRefused, path, len(b))
	}
	switch crypto.Mode(b[0]) {
	case crypto.ModeAES:
		return crypto.ModeAES, nil
	case crypto.ModeSM4:
		return crypto.ModeSM4, nil
	default:
		return 0, fmt.Errorf("%w: crypto_mode file %s has unknown value 0x%02x", ErrStartupRefused, path, b[0])
	}
}

// WriteCryptoModeFile writes a 1-byte crypto_mode file (0x01 for AES,
// 0x02 for SM4). Mode 0o600 is POSIX-only; on Windows the file
// inherits parent NTFS ACL.
func WriteCryptoModeFile(path string, mode crypto.Mode) error {
	var b [1]byte
	switch mode {
	case crypto.ModeAES:
		b[0] = 0x01
	case crypto.ModeSM4:
		b[0] = 0x02
	default:
		return fmt.Errorf("keystore: unknown crypto mode %q", mode)
	}
	if err := os.WriteFile(path, b[:], 0o600); err != nil {
		return fmt.Errorf("keystore: write crypto_mode file: %w", err)
	}
	return nil
}

// kekSalt is a compile-time constant: 16 bytes fixed at binary build time.
// Acts as domain separation for PBKDF2 (so a password leak from one
// deployment doesn't help attackers reuse the same hash in another).
// Not a secret — the literal is in the binary.
var kekSalt = [crypto.SaltSize]byte{
	0x6b, 0x65, 0x6b, 0x2d, 0x73, 0x61, 0x6c, 0x74,
	0x2d, 0x76, 0x31, 0x2e, 0x30, 0x2e, 0x30, 0x00,
}

// DeriveKEK derives the 16-byte KEK from S1+S2 via PBKDF2-HMAC-{SHA256|SM3}.
// password = XOR(s1, s2, S3Shard) (32 bytes); salt = kekSalt.
// PRF is selected by crypto mode: SM3 for ModeSM4, SHA-256 for ModeAES.
func DeriveKEK(s1, s2 [ShardSize]byte, mode crypto.Mode) (*crypto.KeyBytes, error) {
	intermediate := XorThree(s1, s2, S3Shard)
	switch mode {
	case crypto.ModeSM4:
		return crypto.PBKDF2HMAC(intermediate[:], kekSalt[:], sm3.New)
	case crypto.ModeAES:
		return crypto.PBKDF2HMAC(intermediate[:], kekSalt[:], sha256.New)
	default:
		return nil, fmt.Errorf("%w: unknown crypto mode %q", ErrStartupRefused, mode)
	}
}

// LoadSnapshotParams holds inputs needed to load a single KeySnapshot.
type LoadSnapshotParams struct {
	S1Path     string
	S2         [ShardSize]byte
	Mode       crypto.Mode
	KekVersion uint64
	DekVersion uint64
	WrappedDEK []byte
}

// LoadSnapshot reads S1 file + S2 bytes, derives KEK, unwraps DEK, returns a *KeySnapshot.
// Returns ErrStartupRefused if any step fails or WrappedDEK is missing.
func LoadSnapshot(p LoadSnapshotParams) (*KeySnapshot, error) {
	s1, err := LoadS1FromFile(p.S1Path)
	if err != nil {
		return nil, fmt.Errorf("load S1: %w", err)
	}
	kek, err := DeriveKEK(s1, p.S2, p.Mode)
	if err != nil {
		return nil, fmt.Errorf("derive KEK: %w", err)
	}
	if len(p.WrappedDEK) == 0 {
		return nil, fmt.Errorf("%w: no wrapped DEK supplied", ErrStartupRefused)
	}
	dek, err := crypto.UnwrapDEK(p.Mode, kek.Bytes(), p.WrappedDEK)
	if err != nil {
		return nil, fmt.Errorf("%w: unwrap DEK failed: %v", ErrStartupRefused, err)
	}
	if len(dek) != crypto.DEKSize {
		return nil, fmt.Errorf("%w: DEK length %d, want %d", ErrStartupRefused, len(dek), crypto.DEKSize)
	}
	if err := crypto.ProbeUnwrapDEK(p.Mode, kek.Bytes(), dek, p.WrappedDEK); err != nil {
		return nil, fmt.Errorf("%w: probeDecrypt: %v", ErrStartupFatal, err)
	}
	snap := &KeySnapshot{
		KEK:        crypto.NewKeyBytes(kek.Bytes()),
		DEK:        crypto.NewKeyBytes(dek),
		KekVersion: p.KekVersion,
		DekVersion: p.DekVersion,
		CryptoMode: p.Mode,
	}
	kek.Zero()
	return snap, nil
}

// LoadFromDir loads key material from secrets dir + DB, returns a ready-to-use Manager.
// All failures map to ErrStartupRefused. Caller should FATAL-exit on error.
//
// Checks (in order):
//  1. crypto_mode file present + 1B + valid value
//  2. key_metadata row present in DB
//  3. file crypto_mode matches DB crypto_mode — protects against a copy-paste
//     recovery where someone restored the DB from backup but kept the wrong
//     crypto_mode file (or vice versa)
//  4. S1 file present + 32B + mode 0600
//  5. S2 read from km.ActiveConfigShard — S2 lives in the DB (not a file)
//     so the rotating operator's config can change without touching shards
//  6. KEK derives successfully
//  7. DEK unwraps successfully
//
// If pending_kek_version > 0 or pending_dek_version > 0, also loads pending
// snapshot (using the same S1 since file shard hasn't rotated yet).
func LoadFromDir(ctx context.Context, secretsDir, dataDir string, s *store.Store) (*Manager, error) {
	_ = dataDir // reserved for future use (lock file path resolution)

	// DB-first check: if key_metadata row doesn't exist, this is a fresh
	// install (secrets dir will also be empty in that case). Return the
	// dedicated ErrNotInitialized sentinel so the caller (main.go::
	// bootstrapKeystore) can branch into SelfInit. Previously this function
	// consulted the file system first via ReadCryptoModeFile, which would
	// fail with "read crypto_mode file: no such file" on a fresh install —
	// masking the real signal (DB empty) with a corruption-looking error.
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			return nil, fmt.Errorf("%w: key_metadata missing", ErrNotInitialized)
		}
		return nil, fmt.Errorf("%w: load key_metadata: %w", ErrStartupRefused, err)
	}

	modeFile := filepath.Join(secretsDir, "crypto_mode")
	fileMode, err := ReadCryptoModeFile(modeFile)
	if err != nil {
		// DB has data but crypto_mode file missing/wrong — corruption.
		// Caller must NOT branch into SelfInit here; the DB has rows that
		// would be orphaned if we generated a new keystore.
		return nil, err
	}
	if fileMode != km.CryptoMode {
		return nil, fmt.Errorf("%w: crypto_mode mismatch (file=%s, db=%s) — restore DB from backup and restart", ErrStartupRefused, fileMode, km.CryptoMode)
	}
	var activeS2, pendingS2 [ShardSize]byte
	copy(activeS2[:], km.ActiveConfigShard)
	active, err := LoadSnapshot(LoadSnapshotParams{
		S1Path:     S1ShardPath(secretsDir, km.FileShardVersion),
		S2:         activeS2,
		Mode:       fileMode,
		KekVersion: uint64(km.ActiveKekVersion),
		DekVersion: uint64(km.ActiveDekVersion),
		WrappedDEK: km.WrappedDEK,
	})
	if err != nil {
		return nil, err
	}
	mgr := NewManager()
	if km.PendingKekVersion > 0 || km.PendingDekVersion > 0 {
		copy(pendingS2[:], km.PendingConfigShard)
		pending, perr := LoadSnapshot(LoadSnapshotParams{
			S1Path:     S1ShardPath(secretsDir, km.FileShardVersion),
			S2:         pendingS2,
			Mode:       fileMode,
			KekVersion: uint64(km.PendingKekVersion),
			DekVersion: uint64(km.PendingDekVersion),
			WrappedDEK: km.PendingWrappedDEK,
		})
		if perr != nil {
			return nil, fmt.Errorf("%w: load pending snapshot: %v", ErrStartupRefused, perr)
		}
		mgr.InstallDualSnap(active, pending)
	} else {
		mgr.InstallDualSnap(active, nil)
	}
	return mgr, nil
}

// EnsureErrStartupRefused unwraps to ErrStartupRefused if err or any wrap is it.
// Useful for callers that want to type-check LoadFromDir failures.
func EnsureErrStartupRefused(err error) bool {
	return errors.Is(err, ErrStartupRefused)
}
