// Package cache provides an LRU + tombstone cache for credential lookups.
//
// Concurrency model:
//   - Each cache slot holds its Entry in an atomic.Pointer. Get is fully
//     lock-free; Put and PutTombstone use a CAS retry loop. Put refuses to
//     overwrite a non-expired tombstone — once an entry is deleted, no new
//     Put for the same key can succeed until the tombstone TTL elapses.
//   - lruMu guards only the LRU linked-list and the tombstone-quota counter.
//     It is taken briefly on Put/PutTombstone for metadata updates and on
//     Evict/ClearAll for full sweeps. The LRU order is approximate under
//     concurrency, which is fine for a credential cache — strict LRU would
//     only matter if cache eviction actually drove miss rates, which it
//     doesn't in steady state.
//   - hits/misses/tombCount are atomics; Stats/Len take no lock.
package cache

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"
)

// CachedCredential is the value held in a slot for one proxy_key.
type CachedCredential struct {
	ProxyKey string
	APIKey   string
	AuthType string
}

// EntryKind classifies the outcome of a cache lookup.
type EntryKind int

const (
	// EntryMiss means the proxyKey is absent (or its entry expired).
	EntryMiss EntryKind = iota
	// EntryHit means a live credential entry was found.
	EntryHit
	// EntryTombstone means a delete marker is in effect.
	EntryTombstone
)

// CredentialCache is the proxy-key indexed cache contract consumed by the
// CachedCredentialGetter facade and by admin/proxy wiring. Every method keys
// on the opaque proxy_key string; CacheKeyFromProxyKey is identity, so no
// hashing is applied.
type CredentialCache interface {
	Get(proxyKey string) (CachedCredential, error)
	Peek(proxyKey string) (CachedCredential, error)
	Put(proxyKey string, cred CachedCredential) error
	PutTombstone(proxyKey string) error
	Delete(proxyKey string) error
	WriteThrough(ctx context.Context, proxyKey string, op func() error) error
}

// Entry is the value held in a slot. Entries are never mutated in place;
// Put and PutTombstone always install a fresh *Entry so atomic.Pointer
// swaps remain safe.
type Entry struct {
	Cred      *CachedCredential
	ExpiresAt time.Time
	Tombstone bool
}

var ErrCacheMiss = errors.New("cache: miss")
var ErrCacheTombstone = errors.New("cache: tombstone")

type Config struct {
	MaxEntries   int
	TombstoneTTL time.Duration
	EntryTTL     time.Duration
	TombstoneMax int
}

type slot struct {
	entry atomic.Pointer[Entry]
	elem  *list.Element
}

type listElement struct {
	key string
	sl  *slot
}

// InMemoryCredentialCache is the LRU + tombstone implementation of
// CredentialCache.
type InMemoryCredentialCache struct {
	lruMu sync.Mutex
	lru   *list.List
	// entries maps key -> *slot. Tests range over this field directly.
	entries sync.Map

	config Config
	ttl    time.Duration

	tombCount atomic.Int64

	hits   atomic.Uint64
	misses atomic.Uint64
}

// NewInMemoryCredentialCache constructs the in-memory CredentialCache.
func NewInMemoryCredentialCache(config Config) *InMemoryCredentialCache {
	if config.MaxEntries <= 0 {
		config.MaxEntries = 10000
	}
	if config.TombstoneTTL <= 0 {
		config.TombstoneTTL = time.Hour
	}
	if config.EntryTTL <= 0 {
		config.EntryTTL = 10 * time.Minute
	}
	if config.TombstoneMax <= 0 {
		config.TombstoneMax = 1000
	}
	return &InMemoryCredentialCache{
		lru:    list.New(),
		config: config,
		ttl:    config.EntryTTL,
	}
}

// CacheKeyFromProxyKey derives the internal map key for a proxyKey. The
// proxyKey is already a globally unique, unguessable string, so it is used
// directly as the map key without hashing.
func CacheKeyFromProxyKey(proxyKey string) string {
	return proxyKey
}

func (c *InMemoryCredentialCache) getOrCreateSlot(key string) *slot {
	if v, ok := c.entries.Load(key); ok {
		return v.(*slot)
	}
	actual, _ := c.entries.LoadOrStore(key, &slot{})
	return actual.(*slot)
}

func (c *InMemoryCredentialCache) Get(proxyKey string) (CachedCredential, error) {
	key := CacheKeyFromProxyKey(proxyKey)
	v, ok := c.entries.Load(key)
	if !ok {
		c.misses.Add(1)
		return CachedCredential{}, ErrCacheMiss
	}
	sl := v.(*slot)
	entry := sl.entry.Load()
	if entry == nil {
		c.misses.Add(1)
		return CachedCredential{}, ErrCacheMiss
	}
	now := time.Now()

	if entry.Tombstone {
		if now.Before(entry.ExpiresAt) {
			c.extendTombstoneTTL(sl, now.Add(c.config.TombstoneTTL))
			c.touchLRU(sl)
			c.hits.Add(1)
			return CachedCredential{}, ErrCacheTombstone
		}
		c.removeStale(key, sl)
		c.misses.Add(1)
		return CachedCredential{}, ErrCacheMiss
	}

	if !entry.ExpiresAt.IsZero() && now.After(entry.ExpiresAt) {
		c.removeStale(key, sl)
		c.misses.Add(1)
		return CachedCredential{}, ErrCacheMiss
	}

	c.touchLRU(sl)
	c.hits.Add(1)
	return *entry.Cred, nil
}

// Peek returns the cached entry for proxyKey without falling back to any
// underlying source. Behaves identically to Get for the in-memory cache.
func (c *InMemoryCredentialCache) Peek(proxyKey string) (CachedCredential, error) {
	return c.Get(proxyKey)
}

// LookupByProxyKey classifies the cache state for proxyKey in one call,
// returning the kind alongside the credential value (zero value unless
// EntryHit).
func (c *InMemoryCredentialCache) LookupByProxyKey(proxyKey string) (EntryKind, CachedCredential) {
	cred, err := c.Get(proxyKey)
	if err == nil {
		return EntryHit, cred
	}
	if err == ErrCacheTombstone {
		return EntryTombstone, CachedCredential{}
	}
	return EntryMiss, CachedCredential{}
}

func (c *InMemoryCredentialCache) extendTombstoneTTL(sl *slot, newExp time.Time) {
	for {
		old := sl.entry.Load()
		if old == nil || !old.Tombstone || time.Now().After(old.ExpiresAt) {
			return
		}
		newE := *old
		newE.ExpiresAt = newExp
		if sl.entry.CompareAndSwap(old, &newE) {
			return
		}
	}
}

func (c *InMemoryCredentialCache) removeStale(key string, sl *slot) {
	c.lruMu.Lock()
	defer c.lruMu.Unlock()
	if cur, ok := c.entries.Load(key); ok && cur.(*slot) == sl {
		if e := sl.entry.Load(); e != nil && e.Tombstone {
			c.tombCount.Add(-1)
		}
		c.entries.Delete(key)
		if sl.elem != nil {
			c.lru.Remove(sl.elem)
			sl.elem = nil
		}
	}
}

func (c *InMemoryCredentialCache) Put(proxyKey string, cred CachedCredential) error {
	return c.put(proxyKey, &Entry{Cred: &cred, ExpiresAt: time.Now().Add(c.ttl)})
}

func (c *InMemoryCredentialCache) put(key string, newE *Entry) error {
	sl := c.getOrCreateSlot(key)
	for {
		old := sl.entry.Load()
		if old != nil && old.Tombstone && time.Now().Before(old.ExpiresAt) {
			return ErrCacheTombstone
		}
		if sl.entry.CompareAndSwap(old, newE) {
			c.lruMu.Lock()
			c.attachLRULocked(key, sl)
			c.enforceCapacityLocked()
			c.lruMu.Unlock()
			return nil
		}
	}
}

func (c *InMemoryCredentialCache) PutTombstone(proxyKey string) error {
	c.putTombstone(proxyKey)
	return nil
}

func (c *InMemoryCredentialCache) putTombstone(key string) {
	sl := c.getOrCreateSlot(key)
	now := time.Now()
	newExp := now.Add(c.config.TombstoneTTL)
	for {
		old := sl.entry.Load()
		if old != nil && old.Tombstone {
			if now.After(old.ExpiresAt) {
				fresh := &Entry{Tombstone: true, ExpiresAt: newExp}
				if sl.entry.CompareAndSwap(old, fresh) {
					c.lruMu.Lock()
					c.attachLRULocked(key, sl)
					c.enforceCapacityLocked()
					c.lruMu.Unlock()
					return
				}
				continue
			}
			ext := *old
			ext.ExpiresAt = newExp
			sl.entry.CompareAndSwap(old, &ext)
			return
		}
		c.lruMu.Lock()
		if c.tombCount.Load() >= int64(c.config.TombstoneMax) {
			c.evictOldestTombstoneLocked()
		}
		c.lruMu.Unlock()
		installed := &Entry{Tombstone: true, ExpiresAt: newExp}
		if sl.entry.CompareAndSwap(old, installed) {
			c.tombCount.Add(1)
			c.lruMu.Lock()
			c.attachLRULocked(key, sl)
			c.enforceCapacityLocked()
			c.lruMu.Unlock()
			return
		}
	}
}

func (c *InMemoryCredentialCache) attachLRULocked(key string, sl *slot) {
	if sl.elem != nil {
		c.lru.MoveToFront(sl.elem)
		return
	}
	sl.elem = c.lru.PushFront(&listElement{key: key, sl: sl})
}

func (c *InMemoryCredentialCache) enforceCapacityLocked() {
	for c.lru.Len() > c.config.MaxEntries {
		back := c.lru.Back()
		if back == nil {
			return
		}
		le := back.Value.(*listElement)
		c.entries.Delete(le.key)
		if e := le.sl.entry.Load(); e != nil && e.Tombstone {
			c.tombCount.Add(-1)
		}
		c.lru.Remove(back)
		le.sl.elem = nil
	}
}

func (c *InMemoryCredentialCache) touchLRU(sl *slot) {
	c.lruMu.Lock()
	if sl.elem != nil {
		c.lru.MoveToFront(sl.elem)
	}
	c.lruMu.Unlock()
}

func (c *InMemoryCredentialCache) evictOldestTombstoneLocked() bool {
	var oldestKey string
	var oldestSlot *slot
	var oldestTime time.Time
	found := false
	c.entries.Range(func(k, v any) bool {
		e := v.(*slot).entry.Load()
		if e == nil || !e.Tombstone {
			return true
		}
		if !found || e.ExpiresAt.Before(oldestTime) {
			oldestKey = k.(string)
			oldestSlot = v.(*slot)
			oldestTime = e.ExpiresAt
			found = true
		}
		return true
	})
	if !found {
		return false
	}
	c.entries.Delete(oldestKey)
	if oldestSlot.elem != nil {
		c.lru.Remove(oldestSlot.elem)
		oldestSlot.elem = nil
	}
	c.tombCount.Add(-1)
	return true
}

func (c *InMemoryCredentialCache) Delete(proxyKey string) error {
	c.evict(proxyKey)
	return nil
}

func (c *InMemoryCredentialCache) evict(key string) {
	c.lruMu.Lock()
	defer c.lruMu.Unlock()
	v, ok := c.entries.LoadAndDelete(key)
	if !ok {
		return
	}
	sl := v.(*slot)
	if e := sl.entry.Load(); e != nil && e.Tombstone {
		c.tombCount.Add(-1)
	}
	if sl.elem != nil {
		c.lru.Remove(sl.elem)
		sl.elem = nil
	}
}

func (c *InMemoryCredentialCache) Stats() (hits, misses uint64) {
	return c.hits.Load(), c.misses.Load()
}

func (c *InMemoryCredentialCache) Len() int {
	n := 0
	c.entries.Range(func(_, _ any) bool {
		n++
		return true
	})
	return n
}

func (c *InMemoryCredentialCache) WriteThrough(ctx context.Context, key string, op func() error) error {
	var err error
	for attempt := 1; attempt <= maxWriteAttempts; attempt++ {
		if attempt > 1 {
			d := time.Duration(attempt-1)*writeBase +
				time.Duration(rand.Int63n(int64(writeJitter)))
			t := time.NewTimer(d)
			select {
			case <-ctx.Done():
				t.Stop()
				return ctx.Err()
			case <-t.C:
			}
		}
		if err = op(); err == nil {
			return nil
		}
	}
	slog.Warn("cache: WriteThrough failed; evicting", "key", redactKey(key), "attempts", maxWriteAttempts, "err", err)
	c.Delete(key)
	return fmt.Errorf("cache: WriteThrough failed for key=%s after %d attempts: %w", redactKey(key), maxWriteAttempts, err)
}

// WriteThrough retry policy mirrors credentials.go::jitteredBackoff so both
// retry loops in the credmgr subsystem share one tuning knob. SQLite row
// lock and BUSY windows are sub-ms to a few ms; 1ms × (attempt-1) base +
// [0, 2ms) jitter is sufficient to break lock-step while keeping total
// worst-case latency ~20ms (was 350ms).
const (
	maxWriteAttempts = 4
	writeBase        = 1 * time.Millisecond
	writeJitter      = 2 * time.Millisecond
)

// redactKey returns a logging-safe prefix of s. For len > 8 it returns
// the first 8 chars; for len ≤ 8 it returns ~half, so short inputs
// never leak in full even if a future key format permits them.
func redactKey(s string) string {
	n := len(s)
	if n == 0 {
		return ""
	}
	if n > 8 {
		return s[:8]
	}
	return s[:(n+1)/2]
}
