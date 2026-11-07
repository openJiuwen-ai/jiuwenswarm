//go:build cgo

package keystore_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	_ "github.com/mattn/go-sqlite3"
)

// TestSelfInitFreshInstallAES verifies that SelfInit generates S1, crypto_mode,
// S2, and DEK from scratch when no files exist on disk — the "script only
// provides the directory" path.
func TestSelfInitFreshInstallAES(t *testing.T) {
	secretsDir := t.TempDir()
	dataDir := t.TempDir()

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{
		SecretsDir: secretsDir,
		CryptoMode: "aes",
	}, s)
	if err != nil {
		t.Fatalf("SelfInit: %v", err)
	}
	if mgr == nil {
		t.Fatal("mgr is nil")
	}

	// s1.bin.1 should have been written by SelfInit
	s1Path := filepath.Join(secretsDir, "s1.bin.1")
	s1Data, err := os.ReadFile(s1Path)
	if err != nil {
		t.Fatalf("s1.bin.1 not created: %v", err)
	}
	if len(s1Data) != keystore.ShardSize {
		t.Fatalf("s1.bin.1 size = %d, want %d", len(s1Data), keystore.ShardSize)
	}

	// crypto_mode should have been written with 0x01 (AES)
	modePath := filepath.Join(secretsDir, "crypto_mode")
	modeData, err := os.ReadFile(modePath)
	if err != nil {
		t.Fatalf("crypto_mode not created: %v", err)
	}
	if len(modeData) != 1 || modeData[0] != byte(crypto.ModeAES) {
		t.Fatalf("crypto_mode = %v, want [0x01]", modeData)
	}

	snap := mgr.Current()
	if snap == nil {
		t.Fatal("Current() is nil")
	}
	if snap.CryptoMode != crypto.ModeAES {
		t.Fatalf("CryptoMode = %v, want ModeAES", snap.CryptoMode)
	}
	if snap.KekVersion != 1 || snap.DekVersion != 1 {
		t.Fatalf("versions = (%d,%d), want (1,1)", snap.KekVersion, snap.DekVersion)
	}
}

// TestSelfInitFreshInstallSM4 verifies SM4 mode works end-to-end.
func TestSelfInitFreshInstallSM4(t *testing.T) {
	secretsDir := t.TempDir()
	dataDir := t.TempDir()

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{
		SecretsDir: secretsDir,
		CryptoMode: "sm4",
	}, s)
	if err != nil {
		t.Fatalf("SelfInit: %v", err)
	}

	modePath := filepath.Join(secretsDir, "crypto_mode")
	modeData, err := os.ReadFile(modePath)
	if err != nil {
		t.Fatalf("crypto_mode not created: %v", err)
	}
	if len(modeData) != 1 || modeData[0] != byte(crypto.ModeSM4) {
		t.Fatalf("crypto_mode = %v, want [0x02]", modeData)
	}

	snap := mgr.Current()
	if snap.CryptoMode != crypto.ModeSM4 {
		t.Fatalf("CryptoMode = %v, want ModeSM4", snap.CryptoMode)
	}
}

// TestSelfInitReadsExistingS1 verifies that if s1.bin.1 already exists
// (operator pre-placement or crash recovery), SelfInit reads it instead of
// overwriting. crypto_mode is still written if missing.
func TestSelfInitReadsExistingS1(t *testing.T) {
	secretsDir := t.TempDir()
	dataDir := t.TempDir()

	var s1 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	s1Path := filepath.Join(secretsDir, "s1.bin.1")
	if err := os.WriteFile(s1Path, s1[:], 0o600); err != nil {
		t.Fatal(err)
	}

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{
		SecretsDir: secretsDir,
		CryptoMode: "aes",
	}, s)
	if err != nil {
		t.Fatalf("SelfInit: %v", err)
	}
	if mgr == nil {
		t.Fatal("mgr is nil")
	}

	// s1.bin.1 should NOT have been overwritten
	s1Read, err := os.ReadFile(s1Path)
	if err != nil {
		t.Fatal(err)
	}
	for i, b := range s1Read {
		if b != byte(i) {
			t.Fatalf("s1 byte %d = %v, want %v (file was overwritten)", i, b, byte(i))
		}
	}

	// crypto_mode should have been created (was missing)
	modePath := filepath.Join(secretsDir, "crypto_mode")
	if _, err := os.Stat(modePath); err != nil {
		t.Fatalf("crypto_mode not created: %v", err)
	}
}

// TestSelfInitEmptyCryptoModeDefaultsAES verifies that an empty CryptoMode
// string defaults to AES.
func TestSelfInitEmptyCryptoModeDefaultsAES(t *testing.T) {
	secretsDir := t.TempDir()
	dataDir := t.TempDir()

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{
		SecretsDir: secretsDir,
	}, s)
	if err != nil {
		t.Fatalf("SelfInit: %v", err)
	}

	modePath := filepath.Join(secretsDir, "crypto_mode")
	modeData, err := os.ReadFile(modePath)
	if err != nil {
		t.Fatalf("crypto_mode not created: %v", err)
	}
	if modeData[0] != byte(crypto.ModeAES) {
		t.Fatalf("crypto_mode = %v, want [0x01]", modeData)
	}

	if mgr.Current().CryptoMode != crypto.ModeAES {
		t.Fatalf("CryptoMode = %v, want ModeAES", mgr.Current().CryptoMode)
	}
}
