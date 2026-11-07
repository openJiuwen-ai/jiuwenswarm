//go:build cgo

package credmgr

import (
	"context"
	"errors"
	"fmt"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/platform"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
)

// ErrCredentialNotFound is returned when no credential matches the requested
// proxy_key. The sentinel is the single source of truth for miss semantics
// across all CredentialGetter implementations (proxy cache tombstones, admin
// handlers, and the production CredMgr).
var ErrCredentialNotFound = errors.New("credential not found")

// CredMgr is the production credential getter backed by the encrypted SQLite
// store and the keystore Manager.
type CredMgr struct {
	store   *store.Store
	manager *keystore.Manager
}

// NewCredMgr constructs the production credential getter.
func NewCredMgr(s *store.Store, m *keystore.Manager) *CredMgr {
	return &CredMgr{store: s, manager: m}
}

// GetCredentialByProxyKey decrypts and returns the plaintext API key for the
// given proxy_key using dual-snapshot fallback:
//
//	try current DEK → on failure try previous DEK → on both failures return ErrCredentialNotFound.
//
// Implements the cache.Getter interface (no context — store calls use Background).
func (v *CredMgr) GetCredentialByProxyKey(proxyKey string) (apiKey, authType string, err error) {
	ctx := context.Background()

	snap := v.manager.Capture()
	defer v.manager.Release(snap)

	if snap == nil {
		return "", "", fmt.Errorf("credmgr: no active key snapshot")
	}

	cred, err := v.store.GetCredentialByProxyKey(ctx, proxyKey)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			return "", "", ErrCredentialNotFound
		}
		return "", "", fmt.Errorf("credmgr: lookup: %w", err)
	}

	plaintext, err := crypto.DecryptCredential(snap.CryptoMode, snap.DEK.Bytes(), cred.APIKeyCipher)
	if err == nil {
		return string(plaintext), cred.AuthType, nil
	}

	// Try previous DEK: handles in-flight DEK rotation where some rows are
	// still encrypted with the old DEK while new writes use the new one.
	// The Manager keeps the old snapshot live (refCount>0 from pre-swap
	// inflight CRUDs) until DrainCheck confirms it can be released.
	prev := v.manager.Previous()
	if prev == nil {
		return "", "", fmt.Errorf("%w: %v", ErrCredentialNotFound, err)
	}
	plaintext, derr := crypto.DecryptCredential(prev.CryptoMode, prev.DEK.Bytes(), cred.APIKeyCipher)
	if derr != nil {
		return "", "", fmt.Errorf("%w: %v", ErrCredentialNotFound, derr)
	}
	return string(plaintext), cred.AuthType, nil
}
