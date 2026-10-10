package cache

import (
	"context"
	"fmt"
	"log/slog"

	"credential_router/internal/credmgr"
)

// Getter is the underlying credential source (e.g., the encrypted SQL store
// via credmgr.CredMgr). It resolves a proxy_key to plaintext.
// Returns (apiKey, authType, error). error matches credmgr.ErrCredentialNotFound on miss.
type Getter interface {
	GetCredentialByProxyKey(proxyKey string) (apiKey, authType string, err error)
}

// CachedCredentialGetter wraps a Getter with cache lookup, keyed on proxy_key.
type CachedCredentialGetter struct {
	cache            CredentialCache
	getter           Getter
	writeThroughFail bool // test-only: if true, WriteThrough returns an error
}

// NewCachedCredentialGetter creates a new CachedCredentialGetter.
func NewCachedCredentialGetter(c CredentialCache, g Getter) *CachedCredentialGetter {
	return &CachedCredentialGetter{cache: c, getter: g}
}

// PutTombstone marks proxyKey as deleted so subsequent lookups short-circuit
// to credmgr.ErrCredentialNotFound until the tombstone TTL expires.
func (c *CachedCredentialGetter) PutTombstone(proxyKey string) {
	c.cache.PutTombstone(CacheKeyFromProxyKey(proxyKey))
}

// Invalidate evicts any cached entry for proxyKey.
func (c *CachedCredentialGetter) Invalidate(proxyKey string) {
	c.cache.Delete(CacheKeyFromProxyKey(proxyKey))
}

// Peek returns the cached entry for proxyKey without falling back to the
// underlying getter. Returns cache.ErrCacheMiss on miss/tombstone.
func (c *CachedCredentialGetter) Peek(proxyKey string) (*CachedCredential, error) {
	cred, err := c.cache.Get(CacheKeyFromProxyKey(proxyKey))
	if err != nil {
		return nil, err
	}
	return &cred, nil
}

// WriteThrough writes a credential into the cache with retry+backoff.
// Returns the last error if all attempts fail.
func (c *CachedCredentialGetter) WriteThrough(ctx context.Context, proxyKey string, cred *CachedCredential) error {
	if c.writeThroughFail {
		return fmt.Errorf("cache: simulated WriteThrough failure")
	}
	key := CacheKeyFromProxyKey(proxyKey)
	return c.cache.WriteThrough(ctx, key, func() error {
		return c.cache.Put(key, *cred)
	})
}

// GetCredentialByProxyKey returns (apiKey, authType) for the requested proxy_key.
// Cache hit → return cached.
// Cache miss → call getter, cache result, return.
// Cache tombstone → return credmgr.ErrCredentialNotFound.
// Getter returns ErrCredentialNotFound → cache tombstone + return same error.
// Any other getter error → evict cache entry + propagate the error verbatim
// so the caller (proxy handler) can map it to the right HTTP status.
func (c *CachedCredentialGetter) GetCredentialByProxyKey(proxyKey string) (apiKey, authType string, err error) {
	key := CacheKeyFromProxyKey(proxyKey)

	cred, err := c.cache.Get(key)
	if err == nil {
		return cred.APIKey, cred.AuthType, nil
	}
	if err == ErrCacheTombstone {
		return "", "", credmgr.ErrCredentialNotFound
	}
	if err != ErrCacheMiss {
		slog.Warn("cache: unexpected Get error", "key", redactKey(key), "err", err)
		c.cache.Delete(key)
	}

	apiKey, authType, err = c.getter.GetCredentialByProxyKey(proxyKey)
	if err == credmgr.ErrCredentialNotFound {
		c.cache.PutTombstone(key)
		return "", "", err
	}
	if err != nil {
		slog.Warn("cache: getter error; evicting", "key", redactKey(key), "err", err)
		c.cache.Delete(key)
		return "", "", err
	}

	c.cache.Put(key, CachedCredential{
		ProxyKey: proxyKey,
		APIKey:   apiKey, AuthType: authType,
	})
	return apiKey, authType, nil
}
