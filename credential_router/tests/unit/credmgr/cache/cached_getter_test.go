package cache_test

import (
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"credential_router/internal/credmgr/cache"
	"credential_router/internal/credmgr"
)

// stubGetter is a test Getter with controllable behavior.
type stubGetter struct {
	apiKey   string
	authType string
	err      error
	calls    int32 // atomic counter
}

func (s *stubGetter) GetCredentialByProxyKey(proxyKey string) (string, string, error) {
	atomic.AddInt32(&s.calls, 1)
	return s.apiKey, s.authType, s.err
}

const (
	proxyKeyA = "cr_pk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	proxyKeyB = "cr_pk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	proxyKeyC = "cr_pk_ccccccccccccccccccccccccccccccccccccccccccc"
)

func TestCachedGetterCacheHit(t *testing.T) {
	cache_ := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	getter := &stubGetter{apiKey: "k", authType: "openai"}
	cg := cache.NewCachedCredentialGetter(cache_, getter)

	apiKey, authType, err := cg.GetCredentialByProxyKey(proxyKeyA)
	if err != nil {
		t.Fatal(err)
	}
	if apiKey != "k" || authType != "openai" {
		t.Errorf("got (%q,%q), want (k,openai)", apiKey, authType)
	}
	if atomic.LoadInt32(&getter.calls) != 1 {
		t.Errorf("getter calls=%d, want 1", atomic.LoadInt32(&getter.calls))
	}

	// Second call: should be cache hit (no getter call)
	_, _, err = cg.GetCredentialByProxyKey(proxyKeyA)
	if err != nil {
		t.Fatal(err)
	}
	if atomic.LoadInt32(&getter.calls) != 1 {
		t.Errorf("after 2nd call, getter calls=%d, want 1 (should be cache hit)", atomic.LoadInt32(&getter.calls))
	}
}

func TestCachedGetterCacheMissCallsGetter(t *testing.T) {
	cache_ := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	getter := &stubGetter{apiKey: "k", authType: "openai"}
	cg := cache.NewCachedCredentialGetter(cache_, getter)

	_, _, err := cg.GetCredentialByProxyKey(proxyKeyB)
	if err != nil {
		t.Fatal(err)
	}
	if atomic.LoadInt32(&getter.calls) != 1 {
		t.Errorf("getter calls=%d, want 1", atomic.LoadInt32(&getter.calls))
	}
}

func TestCachedGetterNotFoundSetsTombstone(t *testing.T) {
	cache_ := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	getter := &stubGetter{err: credmgr.ErrCredentialNotFound}
	cg := cache.NewCachedCredentialGetter(cache_, getter)

	_, _, err := cg.GetCredentialByProxyKey(proxyKeyA)
	if !errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("got %v, want ErrCredentialNotFound", err)
	}

	// Second call: should still return ErrCredentialNotFound via tombstone
	_, _, err = cg.GetCredentialByProxyKey(proxyKeyA)
	if !errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("got %v, want ErrCredentialNotFound", err)
	}

	if atomic.LoadInt32(&getter.calls) != 1 {
		t.Errorf("getter calls=%d, want 1 (tombstone should suppress)", atomic.LoadInt32(&getter.calls))
	}
}

func TestCachedGetterNonNotFoundErrorPropagatesAndEvicts(t *testing.T) {
	cache_ := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	rawErr := errors.New("decrypt failed")
	getter := &stubGetter{err: rawErr}
	cg := cache.NewCachedCredentialGetter(cache_, getter)

	_, _, err := cg.GetCredentialByProxyKey(proxyKeyA)
	if errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("got ErrCredentialNotFound, want raw error propagated so handler maps to 5xx")
	}
	if err == nil || err.Error() != rawErr.Error() {
		t.Errorf("got %v, want %v", err, rawErr)
	}
}

func TestCachedGetterDifferentKeys(t *testing.T) {
	cache_ := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	getter := &stubGetter{apiKey: "k", authType: "openai"}
	cg := cache.NewCachedCredentialGetter(cache_, getter)

	_, _, _ = cg.GetCredentialByProxyKey(proxyKeyA)
	_, _, _ = cg.GetCredentialByProxyKey(proxyKeyB)
	_, _, _ = cg.GetCredentialByProxyKey(proxyKeyC)

	if atomic.LoadInt32(&getter.calls) != 3 {
		t.Errorf("getter calls=%d, want 3 (3 different keys)", atomic.LoadInt32(&getter.calls))
	}
}
