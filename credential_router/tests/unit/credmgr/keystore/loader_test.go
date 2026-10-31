//go:build cgo

package keystore_test

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
)

func writeShard(t *testing.T, path string, fill byte) {
	t.Helper()
	if err := os.WriteFile(path, bytes.Repeat([]byte{fill}, keystore.ShardSize), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestReadCryptoModeFileAES(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "crypto_mode")
	if err := os.WriteFile(path, []byte{0x01}, 0o600); err != nil {
		t.Fatal(err)
	}
	mode, err := keystore.ReadCryptoModeFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode != crypto.ModeAES {
		t.Errorf("got %v, want ModeAES", mode)
	}
}

func TestReadCryptoModeFileSM(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "crypto_mode")
	if err := os.WriteFile(path, []byte{0x02}, 0o600); err != nil {
		t.Fatal(err)
	}
	mode, err := keystore.ReadCryptoModeFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode != crypto.ModeSM4 {
		t.Errorf("got %v, want ModeSM4", mode)
	}
}

func TestReadCryptoModeFileMissing(t *testing.T) {
	_, err := keystore.ReadCryptoModeFile("/nonexistent/crypto_mode")
	if err == nil {
		t.Fatal("expected error for missing file")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("expected errors.Is(err, ErrStartupRefused), got %v", err)
	}
}

func TestReadCryptoModeFileWrongSize(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "crypto_mode")
	if err := os.WriteFile(path, []byte{0x01, 0x02}, 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.ReadCryptoModeFile(path)
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("got %v, want ErrStartupRefused", err)
	}
}

func TestReadCryptoModeFileBadMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "crypto_mode")
	if err := os.WriteFile(path, []byte{0x01}, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.ReadCryptoModeFile(path)
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("got %v, want ErrStartupRefused", err)
	}
}

func TestReadCryptoModeFileUnknown(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "crypto_mode")
	if err := os.WriteFile(path, []byte{0xFF}, 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.ReadCryptoModeFile(path)
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("got %v, want ErrStartupRefused", err)
	}
}

func TestWriteCryptoModeFile(t *testing.T) {
	dir := t.TempDir()
	for _, c := range []struct {
		mode crypto.Mode
		want byte
	}{
		{crypto.ModeAES, 0x01},
		{crypto.ModeSM4, 0x02},
	} {
		path := filepath.Join(dir, c.mode.String())
		if err := keystore.WriteCryptoModeFile(path, c.mode); err != nil {
			t.Fatal(err)
		}
		b, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if len(b) != 1 || b[0] != c.want {
			t.Errorf("mode=%s got %v, want [%#x]", c.mode, b, c.want)
		}
	}
}

func TestWriteCryptoModeFileUnknown(t *testing.T) {
	dir := t.TempDir()
	if err := keystore.WriteCryptoModeFile(filepath.Join(dir, "x"), crypto.Mode(0xFF)); err == nil {
		t.Error("expected error for unknown mode")
	}
}

func TestDeriveKEKDeterministic(t *testing.T) {
	s1 := [keystore.ShardSize]byte{1, 2, 3}
	s2 := [keystore.ShardSize]byte{4, 5, 6}
	k1, err := keystore.DeriveKEK(s1, s2, crypto.ModeAES)
	if err != nil {
		t.Fatal(err)
	}
	k2, err := keystore.DeriveKEK(s1, s2, crypto.ModeAES)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(k1.Bytes(), k2.Bytes()) {
		t.Error("PBKDF2 should be deterministic")
	}
}

func TestDeriveKEKDifferentS1(t *testing.T) {
	s2 := [keystore.ShardSize]byte{4, 5, 6}
	k1, _ := keystore.DeriveKEK([keystore.ShardSize]byte{1, 2, 3}, s2, crypto.ModeAES)
	k2, _ := keystore.DeriveKEK([keystore.ShardSize]byte{7, 8, 9}, s2, crypto.ModeAES)
	if bytes.Equal(k1.Bytes(), k2.Bytes()) {
		t.Error("different S1 should give different KEK")
	}
}

func TestDeriveKEKOutputSize(t *testing.T) {
	s1 := [keystore.ShardSize]byte{0x42}
	s2 := [keystore.ShardSize]byte{0x24}
	kek, err := keystore.DeriveKEK(s1, s2, crypto.ModeAES)
	if err != nil {
		t.Fatal(err)
	}
	if kek.Len() != 16 {
		t.Errorf("KEK length = %d, want 16", kek.Len())
	}
}

func TestDeriveKEKSM3Mode(t *testing.T) {
	s1 := [keystore.ShardSize]byte{1, 2, 3}
	s2 := [keystore.ShardSize]byte{4, 5, 6}
	kek, err := keystore.DeriveKEK(s1, s2, crypto.ModeSM4)
	if err != nil {
		t.Fatal(err)
	}
	if kek.Len() != 16 {
		t.Errorf("KEK length = %d, want 16", kek.Len())
	}
}

func TestDeriveKEKSM3DifferentFromAES(t *testing.T) {
	s1 := [keystore.ShardSize]byte{1, 2, 3}
	s2 := [keystore.ShardSize]byte{4, 5, 6}
	kAES, _ := keystore.DeriveKEK(s1, s2, crypto.ModeAES)
	kSM, _ := keystore.DeriveKEK(s1, s2, crypto.ModeSM4)
	if bytes.Equal(kAES.Bytes(), kSM.Bytes()) {
		t.Error("AES-mode and SM3-mode KEK should differ for same input")
	}
}

func TestDeriveKEKUnknownMode(t *testing.T) {
	_, err := keystore.DeriveKEK([keystore.ShardSize]byte{}, [keystore.ShardSize]byte{}, crypto.Mode(0xFF))
	if err == nil {
		t.Error("expected error for unknown crypto mode")
	}
}

func TestInstallDualSnapBoth(t *testing.T) {
	m := keystore.NewManager()
	active := &keystore.KeySnapshot{KekVersion: 1, DekVersion: 1, CryptoMode: crypto.ModeAES}
	pending := &keystore.KeySnapshot{KekVersion: 2, DekVersion: 1, CryptoMode: crypto.ModeAES}
	m.InstallDualSnap(active, pending)
	if m.Current().KekVersion != 1 {
		t.Error("active not set as current")
	}
	if m.Previous().KekVersion != 2 {
		t.Error("pending not set as previous")
	}
	if !m.HasCurrent() {
		t.Error("HasCurrent should be true")
	}
	if !m.HasPrevious() {
		t.Error("HasPrevious should be true")
	}
}

func TestInstallDualSnapNilPending(t *testing.T) {
	m := keystore.NewManager()
	active := &keystore.KeySnapshot{KekVersion: 1, DekVersion: 1, CryptoMode: crypto.ModeAES}
	m.InstallDualSnap(active, nil)
	if m.Current().KekVersion != 1 {
		t.Error("active not set")
	}
	if m.Previous() != nil {
		t.Error("previous should be nil")
	}
	if m.HasPrevious() {
		t.Error("HasPrevious should be false")
	}
}

func refusalFixture(t *testing.T) (secretsDir, dataDir string, s *store.Store, cleanup func()) {
	t.Helper()
	_, _, s, secretsDir, cleanup = setupTestRotator(t)
	dataDir = filepath.Dir(s.Path())
	return secretsDir, dataDir, s, cleanup
}

func TestLoadFromDirRefusesOnMissingCryptoMode(t *testing.T) {
	secretsDir, _, s, cleanup := refusalFixture(t)
	defer cleanup()
	if err := os.Remove(filepath.Join(secretsDir, "crypto_mode")); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrStartupRefused, got nil")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("err = %v, want wraps ErrStartupRefused", err)
	}
}

func TestLoadFromDirRefusesOnCryptoModeMismatch(t *testing.T) {
	secretsDir, _, s, cleanup := refusalFixture(t)
	defer cleanup()
	if err := keystore.WriteCryptoModeFile(filepath.Join(secretsDir, "crypto_mode"), crypto.ModeSM4); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrStartupRefused on crypto_mode mismatch, got nil")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("err = %v, want wraps ErrStartupRefused", err)
	}
}

func TestLoadFromDirRefusesOnMissingS1(t *testing.T) {
	secretsDir, _, s, cleanup := refusalFixture(t)
	defer cleanup()
	matches, _ := filepath.Glob(filepath.Join(secretsDir, "s1*"))
	for _, m := range matches {
		if err := os.Remove(m); err != nil {
			t.Fatal(err)
		}
	}
	_, err := keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrStartupRefused on missing S1, got nil")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("err = %v, want wraps ErrStartupRefused", err)
	}
}

func TestLoadFromDirRefusesOnBadWrappedDEK(t *testing.T) {
	secretsDir, _, s, cleanup := refusalFixture(t)
	defer cleanup()
	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for i := range km.WrappedDEK {
		km.WrappedDEK[i] ^= 0xFF
	}
	if err := s.UpdateKeyMetadata(context.Background(), km); err != nil {
		t.Fatal(err)
	}
	_, err = keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrStartupRefused on corrupt wrapped_dek, got nil")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("err = %v, want wraps ErrStartupRefused", err)
	}
}

func TestProbeUnwrapDEKFromLoaderValid(t *testing.T) {
	mode := crypto.ModeAES
	kek := bytes.Repeat([]byte{0xAA}, crypto.KEKSize)
	dek := bytes.Repeat([]byte{0xBB}, crypto.DEKSize)
	wrapped, err := crypto.WrapDEK(mode, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK: %v", err)
	}
	if err := crypto.ProbeUnwrapDEK(mode, kek, dek, wrapped); err != nil {
		t.Errorf("crypto.ProbeUnwrapDEK failed on valid pair: %v", err)
	}
}

func TestProbeUnwrapDEKFromLoaderInvalidLength(t *testing.T) {
	mode := crypto.ModeAES
	kek := bytes.Repeat([]byte{0xAA}, crypto.KEKSize)
	dek := bytes.Repeat([]byte{0xBB}, 8)
	if err := crypto.ProbeUnwrapDEK(mode, kek, dek, make([]byte, 10)); err == nil {
		t.Error("crypto.ProbeUnwrapDEK should fail on invalid length, got nil")
	}
}

// TestLoadFromDirReturnsNotInitializedOnFreshInstall covers the bug where
// LoadFromDir's first action (ReadCryptoModeFile) would fail with
// "no such file" before it ever consulted the DB. On a fresh install the
// DB has no key_metadata row AND the secrets dir is empty — that's the
// normal bootstrap path, NOT a startup refusal. The caller (main.go)
// should branch into SelfInit based on this signal.
func TestLoadFromDirReturnsNotInitializedOnFreshInstall(t *testing.T) {
	secretsDir := t.TempDir()

	dbPath := filepath.Join(t.TempDir(), "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	_, err = keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrNotInitialized on fresh install, got nil")
	}
	if !errors.Is(err, keystore.ErrNotInitialized) {
		t.Errorf("err = %v, want wraps ErrNotInitialized", err)
	}
	// Fresh install must NOT look like a startup refusal — it's the
	// normal bootstrap path, not corruption or misconfiguration.
	if errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("fresh install should not wrap ErrStartupRefused: %v", err)
	}
}

// TestLoadFromDirStillRefusesOnCorruption ensures we didn't regress the
// case where DB HAS a key_metadata row but the crypto_mode file is gone —
// that's corruption (can't decrypt without the file) and must remain
// ErrStartupRefused, not become a silent SelfInit.
func TestLoadFromDirStillRefusesOnCorruption(t *testing.T) {
	secretsDir, _, s, cleanup := refusalFixture(t)
	defer cleanup()
	if err := os.Remove(filepath.Join(secretsDir, "crypto_mode")); err != nil {
		t.Fatal(err)
	}
	_, err := keystore.LoadFromDir(context.Background(), secretsDir, "", s)
	if err == nil {
		t.Fatal("expected ErrStartupRefused on crypto_mode file missing with seeded DB, got nil")
	}
	if !errors.Is(err, keystore.ErrStartupRefused) {
		t.Errorf("err = %v, want wraps ErrStartupRefused", err)
	}
	// It must NOT downgrade to ErrNotInitialized — DB has data.
	if errors.Is(err, keystore.ErrNotInitialized) {
		t.Errorf("DB-seeded load must not return ErrNotInitialized: %v", err)
	}
}
