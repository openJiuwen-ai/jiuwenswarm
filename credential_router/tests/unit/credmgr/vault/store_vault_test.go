//go:build cgo

package vault_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/credmgr"
)

// setupVaultTest builds a temp-backed store + keystore with one encrypted
// credential. It returns the manager, store, the inserted credential's
// proxy_key (the only lookup key in the proxy_key model), and a cleanup func.
func setupVaultTest(t *testing.T) (*credmgr.CredMgr, *keystore.Manager, *store.Store, string, func()) {
	t.Helper()
	secretsDir := t.TempDir()
	dataDir := t.TempDir()

	var s1, s2 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	for i := range s2 {
		s2[i] = byte(i + 100)
	}

	for _, f := range []struct {
		name string
		data []byte
	}{
		{"s1.bin.1", s1[:]}, {"s2", s2[:]},
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, f.name), f.data, 0o600); err != nil {
			t.Fatalf("write %s: %v", f.name, err)
		}
	}
	if err := keystore.WriteCryptoModeFile(filepath.Join(secretsDir, "crypto_mode"), crypto.ModeAES); err != nil {
		t.Fatal(err)
	}

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{SecretsDir: secretsDir}, s)
	if err != nil {
		t.Fatal(err)
	}

	// Encrypt and insert one credential
	snap := mgr.Current()
	mode := crypto.ModeAES
	cipher, err := crypto.EncryptCredential(mode, snap.DEK.Bytes(), []byte("plain-api-key-12345"))
	if err != nil {
		t.Fatalf("EncryptCredential: %v", err)
	}
	cred := &store.Credential{
		UserID:       "u1",
		APIBase:      "https://api.example.com/v1",
		KeyTag:       "default",
		APIKeyCipher: cipher,
		AuthType:     "openai",
		KekVersion:   int64(snap.KekVersion),
		DekVersion:   int64(snap.DekVersion),
	}
	if err := s.InsertCredential(context.Background(), cred); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	return credmgr.NewCredMgr(s, mgr), mgr, s, cred.ProxyKey, func() { _ = s.Close() }
}

func TestCredMgrDecryptSuccess(t *testing.T) {
	sv, _, _, proxyKey, cleanup := setupVaultTest(t)
	defer cleanup()

	apiKey, authType, err := sv.GetCredentialByProxyKey(proxyKey)
	if err != nil {
		t.Fatalf("GetCredentialByProxyKey: %v", err)
	}
	if apiKey != "plain-api-key-12345" {
		t.Errorf("apiKey=%q, want %q", apiKey, "plain-api-key-12345")
	}
	if authType != "openai" {
		t.Errorf("authType=%q, want openai", authType)
	}
}

func TestCredMgrNotFound(t *testing.T) {
	sv, _, _, _, cleanup := setupVaultTest(t)
	defer cleanup()

	_, _, err := sv.GetCredentialByProxyKey("cr_pk_does-not-exist")
	if !errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("got %v, want ErrCredentialNotFound", err)
	}
}

func TestCredMgrNoActiveSnapshot(t *testing.T) {
	s, err := store.OpenForTesting(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	mgr := keystore.NewManager()
	sv := credmgr.NewCredMgr(s, mgr)

	_, _, err = sv.GetCredentialByProxyKey("cr_pk_no-active-snap")
	if err == nil {
		t.Error("expected error when no active snapshot")
	}
}

func TestCredMgrDecryptFailDualSnapshot(t *testing.T) {
	// Scenario: row encrypted with WRONG DEK → both current and previous
	// decrypt fail → return ErrCredentialNotFound (fallback exhausted).
	sv, mgr, s, _, cleanup := setupVaultTest(t)
	defer cleanup()

	// Insert a row with garbage ciphertext that decrypt will fail on.
	cred := &store.Credential{
		UserID:       "u2",
		APIBase:      "https://api.example.com/v1",
		KeyTag:       "default",
		APIKeyCipher: []byte{0xFF, 0xFE, 0xFD, 0xFC, 0xFB},
		AuthType:     "openai",
	}
	if err := s.InsertCredential(context.Background(), cred); err != nil {
		t.Fatal(err)
	}

	// Inject a previous snapshot so dual-snapshot path is exercised.
	prev := &keystore.KeySnapshot{
		KEK:        crypto.NewKeyBytes(make([]byte, 16)),
		DEK:        crypto.NewKeyBytes(make([]byte, 16)),
		KekVersion: 0,
		DekVersion: 0,
		CryptoMode: crypto.ModeAES,
	}
	mgr.InstallDualSnap(mgr.Current(), prev)

	_, _, err := sv.GetCredentialByProxyKey(cred.ProxyKey)
	if !errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("got %v, want ErrCredentialNotFound", err)
	}
}
