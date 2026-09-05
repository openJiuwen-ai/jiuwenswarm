package keystore

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/store"
)

// SelfInitParams holds the inputs for self-init.
type SelfInitParams struct {
	SecretsDir string
	// CryptoMode is the config-specified cipher mode ("aes" or "sm4").
	// Only used on fresh install when the crypto_mode file doesn't exist
	// yet. Once the file is written, the on-disk value is authoritative
	// and the config field is ignored on subsequent starts.
	CryptoMode string
}

// ErrSelfInitRefused indicates self-init refused (already initialized, or missing files).
var ErrSelfInitRefused = errors.New("keystore: self-init refused")

// SelfInit performs binary self-init when key_metadata is empty — the
// very first run after install, before any rotation has touched the DB.
//
// On fresh install (no crypto_mode file, no s1.bin.1):
//  1. Determine crypto mode from config, write crypto_mode file
//  2. Generate random S1, write s1.bin.1
//
// On crash recovery or operator pre-placed S1 (files already exist):
//  1. Read existing crypto_mode file
//  2. Read existing s1.bin.1
//
// Then:
//  3. Generate random S2 (in memory)
//  4. Derive KEK from S1+S2+S3 via PBKDF2
//  5. Generate random DEK (in memory, never written to disk)
//  6. Wrap DEK with KEK
//  7. Verify DB key_metadata is empty
//  8. Insert key_metadata row (stores S2 + wrapped DEK)
//  9. Build Manager with active snapshot
//
// This is idempotent: if the binary crashes after writing files but
// before inserting DB, a restart will find the files and reuse them
// rather than regenerating.
//
// Returns ErrSelfInitRefused if any step fails or if key_metadata already exists.
func SelfInit(ctx context.Context, p SelfInitParams, s *store.Store) (*Manager, error) {
	modeFile := filepath.Join(p.SecretsDir, "crypto_mode")
	fileMode, err := ensureCryptoMode(modeFile, p.CryptoMode)
	if err != nil {
		return nil, fmt.Errorf("%w: crypto_mode: %w", ErrSelfInitRefused, err)
	}

	s1, err := ensureS1(p.SecretsDir)
	if err != nil {
		return nil, fmt.Errorf("%w: s1: %w", ErrSelfInitRefused, err)
	}

	var s2 [ShardSize]byte
	if _, err := io.ReadFull(rand.Reader, s2[:]); err != nil {
		return nil, fmt.Errorf("%w: generate s2: %w", ErrSelfInitRefused, err)
	}

	kek, err := DeriveKEK(s1, s2, fileMode)
	if err != nil {
		return nil, fmt.Errorf("%w: derive KEK: %w", ErrSelfInitRefused, err)
	}

	dekBytes := make([]byte, crypto.DEKSize)
	if _, err := io.ReadFull(rand.Reader, dekBytes); err != nil {
		return nil, fmt.Errorf("%w: generate DEK: %w", ErrSelfInitRefused, err)
	}
	dek := crypto.NewKeyBytes(dekBytes)

	wrappedDEK, err := crypto.WrapDEK(fileMode, kek.Bytes(), dek.Bytes())
	if err != nil {
		return nil, fmt.Errorf("%w: wrap DEK: %w", ErrSelfInitRefused, err)
	}

	_, err = s.GetKeyMetadata(ctx)
	if err == nil {
		return nil, fmt.Errorf("%w: key_metadata already exists in DB", ErrSelfInitRefused)
	}

	now := time.Now().Unix()
	km := &store.KeyMetadata{
		ActiveKekVersion:   1,
		PendingKekVersion:  0,
		ActiveDekVersion:   1,
		PendingDekVersion:  0,
		CryptoMode:         fileMode,
		ActiveConfigShard:  s2[:],
		PendingConfigShard: []byte{},
		FileShardVersion:   1,
		FileShardRotatedAt: now,
		LastRotateAt:       now,
		WrappedDEK:         wrappedDEK,
		PendingWrappedDEK:  []byte{},
		DekRotatedAt:       now,
	}
	if err := s.InsertKeyMetadata(ctx, km); err != nil {
		return nil, fmt.Errorf("%w: insert key_metadata: %w", ErrSelfInitRefused, err)
	}

	active := &KeySnapshot{
		KEK:        crypto.NewKeyBytes(kek.Bytes()),
		DEK:        crypto.NewKeyBytes(dek.Bytes()),
		KekVersion: 1,
		DekVersion: 1,
		CryptoMode: fileMode,
	}
	kek.Zero()
	mgr := NewManager()
	mgr.InstallDualSnap(active, nil)
	return mgr, nil
}

// ensureCryptoMode reads the crypto_mode file if it exists, or creates it
// from the config-specified mode string if it doesn't. This makes SelfInit
// idempotent: a crash after writing the file but before DB insert won't
// regenerate the mode on restart.
func ensureCryptoMode(path, configMode string) (crypto.Mode, error) {
	if _, err := os.Stat(path); err == nil {
		return ReadCryptoModeFile(path)
	} else if !os.IsNotExist(err) {
		return 0, fmt.Errorf("stat crypto_mode: %w", err)
	}

	mode, err := parseCryptoModeString(configMode)
	if err != nil {
		return 0, err
	}
	if err := WriteCryptoModeFile(path, mode); err != nil {
		return 0, fmt.Errorf("write crypto_mode: %w", err)
	}
	return mode, nil
}

// ensureS1 reads s1.bin.1 if it exists, or generates and writes a new
// random S1 if it doesn't. This supports both fresh install (generate)
// and crash recovery / operator pre-placed S1 (read existing).
func ensureS1(secretsDir string) ([ShardSize]byte, error) {
	s1Path := S1ShardPath(secretsDir, 1)
	if _, err := os.Stat(s1Path); err == nil {
		return LoadS1FromFile(s1Path)
	} else if !os.IsNotExist(err) {
		return [ShardSize]byte{}, fmt.Errorf("stat s1: %w", err)
	}

	var s1 [ShardSize]byte
	if _, err := io.ReadFull(rand.Reader, s1[:]); err != nil {
		return [ShardSize]byte{}, fmt.Errorf("generate s1: %w", err)
	}
	if err := WriteS1ToFile(s1Path, s1); err != nil {
		return [ShardSize]byte{}, fmt.Errorf("write s1: %w", err)
	}
	return s1, nil
}

// parseCryptoModeString converts a config string ("aes" or "sm4") to the
// binary mode byte. Empty string defaults to AES.
func parseCryptoModeString(s string) (crypto.Mode, error) {
	switch strings.ToLower(s) {
	case "aes", "":
		return crypto.ModeAES, nil
	case "sm4":
		return crypto.ModeSM4, nil
	default:
		return 0, fmt.Errorf("unknown crypto_mode %q (want aes or sm4)", s)
	}
}
