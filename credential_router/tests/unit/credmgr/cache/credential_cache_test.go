package cache_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"credential_router/internal/credmgr/cache"
)

func sampleCred(proxyKey string) cache.CachedCredential {
	return cache.CachedCredential{ProxyKey: proxyKey, APIKey: "secret", AuthType: "openai"}
}

func TestCachePutGet(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("cr_pk_1"))
	got, err := c.Get("k1")
	if err != nil {
		t.Fatal(err)
	}
	if got.ProxyKey != "cr_pk_1" {
		t.Errorf("got %s, want cr_pk_1", got.ProxyKey)
	}
}

func TestCacheMiss(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	_, err := c.Get("nonexistent")
	if !errors.Is(err, cache.ErrCacheMiss) {
		t.Errorf("got %v, want cache.ErrCacheMiss", err)
	}
}

func TestCacheTombstone(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	c.PutTombstone("k1")
	_, err := c.Get("k1")
	if !errors.Is(err, cache.ErrCacheTombstone) {
		t.Errorf("got %v, want cache.ErrCacheTombstone", err)
	}
}

func TestCacheTombstoneExpires(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: 50 * time.Millisecond})
	c.PutTombstone("k1")
	time.Sleep(100 * time.Millisecond)
	c.Put("k1", sampleCred("cr_pk_1"))
	got, err := c.Get("k1")
	if err != nil {
		t.Fatal(err)
	}
	if got.ProxyKey != "cr_pk_1" {
		t.Error("expected to re-cache after tombstone expired")
	}
}

func TestCacheEvict(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("cr_pk_1"))
	c.Delete("k1")
	_, err := c.Get("k1")
	if !errors.Is(err, cache.ErrCacheMiss) {
		t.Errorf("after delete, got %v, want cache.ErrCacheMiss", err)
	}
}

func TestCacheLRUEviction(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 3, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("u1"))
	c.Put("k2", sampleCred("u2"))
	c.Put("k3", sampleCred("u3"))
	c.Put("k4", sampleCred("u4"))

	if _, err := c.Get("k1"); !errors.Is(err, cache.ErrCacheMiss) {
		t.Error("k1 should have been evicted")
	}
	for _, k := range []string{"k2", "k3", "k4"} {
		if _, err := c.Get(k); err != nil {
			t.Errorf("%s should be present: %v", k, err)
		}
	}
}

func TestCacheLRUOrder(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 3, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("u1"))
	c.Put("k2", sampleCred("u2"))
	c.Put("k3", sampleCred("u3"))
	_, _ = c.Get("k1") // touch k1
	c.Put("k4", sampleCred("u4"))

	if _, err := c.Get("k2"); !errors.Is(err, cache.ErrCacheMiss) {
		t.Error("k2 should have been evicted (LRU)")
	}
	for _, k := range []string{"k1", "k3", "k4"} {
		if _, err := c.Get(k); err != nil {
			t.Errorf("%s should be present: %v", k, err)
		}
	}
}

func TestCacheConcurrency(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 1000, TombstoneTTL: time.Hour})
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := 0; j < 100; j++ {
				key := string(rune('a'+id)) + string(rune('a'+j))
				c.Put(key, sampleCred(string(rune('u'+id))))
				_, _ = c.Get(key)
			}
		}(i)
	}
	wg.Wait()
}

func TestCacheWriteThroughSuccess(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	called := 0
	err := c.WriteThrough(context.Background(), "k1", func() error {
		called++
		return nil
	})
	if err != nil {
		t.Fatalf("WriteThrough returned error: %v", err)
	}
	if called != 1 {
		t.Errorf("op called %d times, want 1", called)
	}
}

func TestCacheWriteThroughRetries(t *testing.T) {
	attempts := 0
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	err := c.WriteThrough(context.Background(), "k1", func() error {
		attempts++
		if attempts < 2 {
			return errors.New("transient")
		}
		return nil
	})
	if err != nil {
		t.Fatalf("WriteThrough returned error after retry: %v", err)
	}
	if attempts != 2 {
		t.Errorf("attempts=%d, want 2", attempts)
	}
}

func TestCacheWriteThroughAllAttemptsFail(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	attempts := 0
	err := c.WriteThrough(context.Background(), "k1", func() error {
		attempts++
		return errors.New("persistent")
	})
	if err == nil {
		t.Fatal("WriteThrough should return error after all attempts fail")
	}
	if attempts != 4 {
		t.Errorf("attempts=%d, want 4", attempts)
	}
}

func TestCacheWriteThroughEvictsOnFailure(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("u1"))
	err := c.WriteThrough(context.Background(), "k1", func() error { return errors.New("persistent") })
	if err == nil {
		t.Fatal("WriteThrough should return error")
	}
	_, cerr := c.Get("k1")
	if !errors.Is(cerr, cache.ErrCacheMiss) {
		t.Errorf("after WriteThrough failure, got %v, want cache.ErrCacheMiss (entry should be evicted)", cerr)
	}
}

func TestCacheKey(t *testing.T) {
	k := cache.CacheKeyFromProxyKey("cr_pk_abcdef")
	if k != "cr_pk_abcdef" {
		t.Errorf("got %q, want %q (proxy_key is the cache key)", k, "cr_pk_abcdef")
	}
}

func TestCacheDefaults(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{})
	c.Put("k1", sampleCred("u1"))
	if c.Len() != 1 {
		t.Error("default config should still work")
	}
}

func TestCacheStats(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: time.Hour})
	c.Put("k1", sampleCred("u1"))
	_, _ = c.Get("k1")
	_, _ = c.Get("k1")
	_, _ = c.Get("missing")
	hits, misses := c.Stats()
	if hits != 2 {
		t.Errorf("hits=%d, want 2", hits)
	}
	if misses != 1 {
		t.Errorf("misses=%d, want 1", misses)
	}
}

func TestCacheGetRemovesExpiredTombstone(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: 50 * time.Millisecond})
	c.PutTombstone("k1")
	if c.Len() != 1 {
		t.Fatalf("Len()=%d before expiry, want 1", c.Len())
	}
	time.Sleep(100 * time.Millisecond)

	_, missesBefore := c.Stats()

	_, err := c.Get("k1")
	if !errors.Is(err, cache.ErrCacheMiss) {
		t.Fatalf("Get on expired tombstone: err=%v, want cache.ErrCacheMiss (not cache.ErrCacheTombstone)", err)
	}
	if n := c.Len(); n != 0 {
		t.Errorf("Len()=%d after expired-tombstone Get, want 0 (removeStale must have evicted)", n)
	}

	c.Put("k1", sampleCred("u1"))
	if _, err := c.Get("k1"); err != nil {
		t.Fatalf("Get after refilling: %v", err)
	}

	_, missesAfter := c.Stats()
	if missesAfter <= missesBefore {
		t.Errorf("misses did not increment after expired-tombstone Get (before=%d, after=%d)", missesBefore, missesAfter)
	}
}

func TestCacheLRUEvictionUnderConcurrency(t *testing.T) {
	const maxEntries = 50
	const goroutines = 8
	const keysPerGoroutine = 500

	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: maxEntries, TombstoneTTL: time.Hour})
	errCh := make(chan error, goroutines*keysPerGoroutine)
	var wg sync.WaitGroup
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(gid int) {
			defer wg.Done()
			for j := 0; j < keysPerGoroutine; j++ {
				key := fmt.Sprintf("k-%d-%d", gid, j)
				cred := sampleCred(fmt.Sprintf("cr_pk-%d-%d", gid, j))
				c.Put(key, cred)
				got, err := c.Get(key)
				if err == nil {
					if got.ProxyKey != cred.ProxyKey {
						errCh <- fmt.Errorf("key %s: hit returned wrong credential %q", key, got.ProxyKey)
					}
				} else if !errors.Is(err, cache.ErrCacheMiss) {
					errCh <- fmt.Errorf("key %s: unexpected err %v", key, err)
				}
			}
		}(g)
	}
	wg.Wait()
	close(errCh)

	for err := range errCh {
		t.Error(err)
	}
	if n := c.Len(); n > maxEntries {
		t.Errorf("Len()=%d, want <= %d (LRU must bound size under contention)", n, maxEntries)
	}
}

func TestPutTombstoneRespectsQuota(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{
		MaxEntries:   1000,
		TombstoneTTL: time.Hour,
		TombstoneMax: 3,
	})
	for i := 0; i < 10; i++ {
		c.PutTombstone(fmt.Sprintf("k%d", i))
	}

	// Quota enforcement evicts the oldest tombstones entirely, so only the
	// most recent TombstoneMax keys still report ErrCacheTombstone; evicted
	// keys fall back to ErrCacheMiss. Counted through public Get behavior.
	countTomb := func() int {
		n := 0
		for i := 0; i < 10; i++ {
			if _, err := c.Get(fmt.Sprintf("k%d", i)); errors.Is(err, cache.ErrCacheTombstone) {
				n++
			}
		}
		return n
	}
	if got := countTomb(); got != 3 {
		t.Errorf("tombstone count=%d, want 3 (TombstoneMax)", got)
	}
}

func TestPutTombstoneRefreshesExistingKey(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{
		MaxEntries:   100,
		TombstoneTTL: 50 * time.Millisecond,
		TombstoneMax: 100,
	})
	c.PutTombstone("k1")
	time.Sleep(30 * time.Millisecond)
	c.PutTombstone("k1")
	time.Sleep(30 * time.Millisecond)

	if _, err := c.Get("k1"); err != cache.ErrCacheTombstone {
		t.Errorf("got err=%v, want cache.ErrCacheTombstone (TTL refreshed on re-Put)", err)
	}
}

func TestGetOnTombstoneExtendsTTL(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{
		MaxEntries:   100,
		TombstoneTTL: 100 * time.Millisecond,
		TombstoneMax: 100,
	})
	c.PutTombstone("k1")
	time.Sleep(60 * time.Millisecond)

	if _, err := c.Get("k1"); err != cache.ErrCacheTombstone {
		t.Fatalf("first Get: err=%v, want cache.ErrCacheTombstone", err)
	}
	time.Sleep(60 * time.Millisecond)

	if _, err := c.Get("k1"); err != cache.ErrCacheTombstone {
		t.Errorf("second Get after 120ms total: err=%v, want cache.ErrCacheTombstone (TTL refreshed by repeated access)", err)
	}
}

func TestEvictTombstoneDecrementsCount(t *testing.T) {
	c := cache.NewInMemoryCredentialCache(cache.Config{
		MaxEntries:   100,
		TombstoneTTL: time.Hour,
		TombstoneMax: 2,
	})
	c.PutTombstone("k1")
	c.Delete("k1")

	// Delete must decrement the internal tombstone quota counter: if it did
	// not, the quota budget would be exhausted and k3's PutTombstone would
	// evict k2, making k2 report ErrCacheMiss. Both k2 and k3 staying
	// tombstoned proves the counter was decremented.
	c.PutTombstone("k2")
	c.PutTombstone("k3")
	if _, err := c.Get("k2"); !errors.Is(err, cache.ErrCacheTombstone) {
		t.Errorf("k2: got %v, want ErrCacheTombstone (Evict must decrement the tombstone quota)", err)
	}
	if _, err := c.Get("k3"); !errors.Is(err, cache.ErrCacheTombstone) {
		t.Errorf("k3: got %v, want ErrCacheTombstone", err)
	}
}
