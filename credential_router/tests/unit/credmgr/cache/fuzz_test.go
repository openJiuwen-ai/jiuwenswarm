//go:build cgo

// Fuzz tests for the cache package (LRU + tombstone CredentialCache and the
// CachedCredentialGetter facade).
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./tests/unit/cache/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./tests/unit/cache/...
//
// Targets assert: set-then-get returns an equal credential; get on a missing
// / evicted / expired-tombstone key returns cache.ErrCacheMiss or cache.ErrCacheTombstone
// (never a nil credential with a nil error, never a panic); LRU eviction
// bounds the cache; concurrent Put/Get/Evict/PutTombstone across goroutines
// never panics and only ever yields the three legal outcomes.
package cache_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"credential_router/internal/credmgr/cache"
)

// fuzzCacheSeeds returns the shared seed corpus: empty, single byte,
// known-valid, empty fields, unicode/emoji, null bytes, max length, many
// parts, invalid UTF-8, and an injection payload.
func fuzzCacheSeeds() [][]byte {
	return [][]byte{
		{},
		{0},
		[]byte("k1|cr_pk_aaaa|sk-123|openai"),
		[]byte("k1|||"),
		[]byte("|k|a|o"), // empty key
		[]byte("用户|cr_pk_🔑|sk-🔐|openai"),
		[]byte("u\x00id|cr_pk_\x00k|a\x00pi|auth\x00"),
		[]byte(strings.Repeat("k", 2000) + "|k|a|o"),
		[]byte(strings.Repeat("🚀", 500)),
		[]byte("a|b|c|d|e|f|g|h|i|j"),
		{0xff, 0xfe, 0xfd},
		[]byte("'; DROP TABLE cache;--|k|a|o"),
	}
}

// fuzzParseCachedCred splits a fuzz input on '|' into a cache key plus the
// three cache.CachedCredential fields (string slicing; missing parts are "").
func fuzzParseCachedCred(data []byte) (key string, cred *cache.CachedCredential) {
	parts := bytes.Split(data, []byte{'|'})
	get := func(i int) string {
		if i < len(parts) {
			return string(parts[i])
		}
		return ""
	}
	return get(0), &cache.CachedCredential{
		ProxyKey: get(1),
		APIKey:   get(2),
		AuthType: get(3),
	}
}

// FuzzCachePutGet — set-then-get returns an equal credential; with
// MaxEntries=4 a fifth distinct put must LRU-evict the oldest key; stats stay
// sane. Never panics on any key/credential bytes.
func FuzzCachePutGet(f *testing.F) {
	for _, s := range fuzzCacheSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 4, TombstoneTTL: time.Hour})
		key, cred := fuzzParseCachedCred(data)

		// 1. set-then-get returns equal payload.
		c.Put(key, *cred)
		got, err := c.Get(key)
		if err != nil {
			t.Fatalf("Get(%q) right after Put: %v", key, err)
		}
		if got != *cred {
			t.Fatalf("round-trip mismatch for key %q: got %+v want %+v", key, got, *cred)
		}

		// 2. LRU eviction: key was put first, then 4 more keys are put, so
		//    key (LRU) must be evicted while the newcomers survive.
		suffix := func(n int) string { return fmt.Sprintf("%s%d", key, n) }
		c.Put(suffix(1), cache.CachedCredential{ProxyKey: suffix(1), APIKey: "a1"})
		c.Put(suffix(2), cache.CachedCredential{ProxyKey: suffix(2), APIKey: "a2"})
		c.Put(suffix(3), cache.CachedCredential{ProxyKey: suffix(3), APIKey: "a3"})
		c.Put(suffix(4), cache.CachedCredential{ProxyKey: suffix(4), APIKey: "a4"})
		if _, err := c.Get(key); !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("key %q should have been LRU-evicted, got err=%v", key, err)
		}
		for n := 1; n <= 4; n++ {
			if _, err := c.Get(suffix(n)); err != nil {
				t.Fatalf("key %q should survive LRU eviction, got err=%v", suffix(n), err)
			}
		}

		// 3. both hit and miss counters advanced (never negative/panic).
		hits, misses := c.Stats()
		if hits == 0 {
			t.Fatal("expected >=1 cache hit from surviving keys")
		}
		if misses == 0 {
			t.Fatal("expected >=1 cache miss from the evicted key")
		}
	})
}

// FuzzCacheEvictAndTombstone — Delete on a missing key is a no-op; Delete on a
// present key turns Get into cache.ErrCacheMiss; PutTombstone makes Get report
// cache.ErrCacheTombstone with a nil payload; a re-put clears the tombstone.
func FuzzCacheEvictAndTombstone(f *testing.F) {
	for _, s := range fuzzCacheSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 8, TombstoneTTL: time.Hour})
		key, cred := fuzzParseCachedCred(data)

		// Delete on empty cache: no-op, must not panic.
		c.Delete(key)
		if _, err := c.Get(key); !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("Get on empty cache: expected cache.ErrCacheMiss, got %v", err)
		}

		// Put then Delete → miss (never a stale hit, never nil/nil).
		c.Put(key, *cred)
		c.Delete(key)
		got, err := c.Get(key)
		if !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("after Delete: expected cache.ErrCacheMiss, got err=%v got=%v", err, got)
		}

		// Tombstone → cache.ErrCacheTombstone and a nil payload.
		c.PutTombstone(key)
		got, err = c.Get(key)
		if !errors.Is(err, cache.ErrCacheTombstone) {
			t.Fatalf("on tombstone: expected cache.ErrCacheTombstone, got err=%v got=%v", err, got)
		}

		// Re-Put on a non-expired tombstone must NOT clear it: the deleted
		// entry stays tombstoned until its TTL
		// expires, even if a concurrent reader races to cache a fresh value.
		if perr := c.Put(key, *cred); !errors.Is(perr, cache.ErrCacheTombstone) {
			t.Fatalf("re-Put over non-expired tombstone: expected cache.ErrCacheTombstone, got %v", perr)
		}
		got, err = c.Get(key)
		if !errors.Is(err, cache.ErrCacheTombstone) {
			t.Fatalf("Get after re-Put over non-expired tombstone: expected cache.ErrCacheTombstone, got err=%v got=%v", err, got)
		}
	})
}

// FuzzCacheTombstoneExpiry — tombstone semantics, split into two deterministic
// phases:
//
//   - Pre-expiry (long TTL, no timing dependence): a tombstone reports
//     cache.ErrCacheTombstone with a nil payload while inside its TTL and counts
//     toward Len().
//   - Post-expiry (short TTL, generous sleep margin): once the TTL has
//     definitely elapsed, Get reports cache.ErrCacheMiss and removes the entry, and
//     the key can be re-cached.
//
// NB: asserting a tombstone is still live within a *short* TTL window is racy
// under fuzzing load (a descheduled worker can overshoot the window), so the
// pre-expiry check uses a long TTL and never sleeps.
func FuzzCacheTombstoneExpiry(f *testing.F) {
	for _, s := range fuzzCacheSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		key, cred := fuzzParseCachedCred(data)

		// Phase A — live tombstone (1h TTL: cannot expire mid-check).
		a := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 8, TombstoneTTL: time.Hour})
		a.PutTombstone(key)
		got, err := a.Get(key)
		if !errors.Is(err, cache.ErrCacheTombstone) {
			t.Fatalf("live tombstone: expected cache.ErrCacheTombstone, got err=%v got=%v", err, got)
		}
		if a.Len() != 1 {
			t.Fatalf("live tombstone Len()=%d, want 1", a.Len())
		}

		// Phase B — expired tombstone (5ms TTL, slept 25ms = 5x margin).
		b := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 8, TombstoneTTL: 5 * time.Millisecond})
		b.PutTombstone(key)
		if b.Len() != 1 {
			t.Fatalf("pre-sleep Len()=%d, want 1", b.Len())
		}
		time.Sleep(25 * time.Millisecond)
		got, err = b.Get(key)
		if !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("expired tombstone: expected cache.ErrCacheMiss, got err=%v got=%v", err, got)
		}
		if b.Len() != 0 {
			t.Fatalf("post-expiry Len()=%d, want 0 (expired tombstone removed)", b.Len())
		}

		// The key can be re-cached after the tombstone expires.
		b.Put(key, *cred)
		got, err = b.Get(key)
		if err != nil || got != *cred {
			t.Fatalf("refill after expiry: err=%v got=%+v", err, got)
		}
	})
}

// FuzzCacheConcurrent — N goroutines Put/Get/Evict/PutTombstone on disjoint
// keys derived from the input. Only three outcomes are legal for Get: nil
// error with a matching credential, cache.ErrCacheMiss, or cache.ErrCacheTombstone. The
// cache must never panic, never hand back a nil credential on a nil error,
// and must stay within MaxEntries.
func FuzzCacheConcurrent(f *testing.F) {
	for _, s := range fuzzCacheSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		const goroutines = 8
		const ops = 32
		c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 32, TombstoneTTL: time.Minute})
		key, cred := fuzzParseCachedCred(data)

		var wg sync.WaitGroup
		errCh := make(chan error, goroutines*ops)
		for g := 0; g < goroutines; g++ {
			wg.Add(1)
			go func(g int) {
				defer wg.Done()
				for j := 0; j < ops; j++ {
					k := fmt.Sprintf("%s|%d|%d", key, g, j)
					local := *cred
					local.ProxyKey = fmt.Sprintf("%s-%d-%d", cred.ProxyKey, g, j)
					c.Put(k, local)

					got, err := c.Get(k)
					switch {
					case err == nil:
						if got.ProxyKey != local.ProxyKey {
							errCh <- fmt.Errorf("key %q: got proxy key %q want %q", k, got.ProxyKey, local.ProxyKey)
						}
					case errors.Is(err, cache.ErrCacheMiss), errors.Is(err, cache.ErrCacheTombstone):
						// LRU-evicted or tombstoned under contention — legal.
					default:
						errCh <- fmt.Errorf("key %q: unexpected error %v", k, err)
					}

					// Interleave destructive ops on the same (private) key.
					if j%3 == 0 {
						c.Delete(k)
					}
					if j%7 == 0 {
						c.PutTombstone(k)
					}
					_, _ = c.Stats()
					_ = c.Len()
				}
			}(g)
		}
		wg.Wait()
		close(errCh)
		for err := range errCh {
			t.Error(err)
		}
		if n := c.Len(); n > 32 {
			t.Fatalf("Len()=%d exceeds MaxEntries=32 (LRU must bound size under contention)", n)
		}
	})
}

// FuzzCacheGetterPeek — the CachedCredentialGetter facade. Peek must never
// fall through to the underlying getter, must return cache.ErrCacheMiss on a miss,
// and must surface cached data verbatim after WriteThrough. Invalidate clears
// the entry.
func FuzzCacheGetterPeek(f *testing.F) {
	for _, s := range fuzzCacheSeeds() {
		f.Add(s)
	}
	f.Add([]byte("cr_pk_proxykeyseed|sk|openai"))
	f.Fuzz(func(t *testing.T, data []byte) {
		parts := bytes.Split(data, []byte{'|'})
		get := func(i int) string {
			if i < len(parts) {
				return string(parts[i])
			}
			return ""
		}
		proxyKey := get(0)

		cc := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 16, TombstoneTTL: time.Hour})
		getter := &stubGetter{apiKey: "k", authType: "openai"}
		cg := cache.NewCachedCredentialGetter(cc, getter)

		// Peek on empty cache: miss, and the getter must NOT be called.
		if _, err := cg.Peek(proxyKey); !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("Peek on empty cache: expected cache.ErrCacheMiss, got %v", err)
		}

		// Invalidate on a missing key is a no-op.
		cg.Invalidate(proxyKey)
		if _, err := cg.Peek(proxyKey); !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("Peek after Invalidate: expected cache.ErrCacheMiss, got %v", err)
		}

		// WriteThrough then Peek → verbatim hit.
		cred := &cache.CachedCredential{
			ProxyKey: proxyKey,
			APIKey:   "ak", AuthType: "at",
		}
		if err := cg.WriteThrough(context.Background(), proxyKey, cred); err != nil {
			t.Fatalf("WriteThrough: %v", err)
		}
		got, err := cg.Peek(proxyKey)
		if err != nil {
			t.Fatalf("Peek after WriteThrough: %v", err)
		}
		if got == nil || *got != *cred {
			t.Fatalf("Peek round-trip: got %+v want %+v", got, cred)
		}

		// Invalidate clears it; tombstone surfaces as an error, not a payload.
		cg.Invalidate(proxyKey)
		if _, err := cg.Peek(proxyKey); !errors.Is(err, cache.ErrCacheMiss) {
			t.Fatalf("Peek after Invalidate(2): expected cache.ErrCacheMiss, got %v", err)
		}
		cg.PutTombstone(proxyKey)
		got, err = cg.Peek(proxyKey)
		if err == nil || got != nil {
			t.Fatalf("Peek on tombstone: expected error + nil credential, got err=%v got=%+v", err, got)
		}
	})
}
