package keystore

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"credential_router/internal/credmgr/crypto"
)

// KeySnapshot holds the key material at a point in time.
// KEK and DEK are *crypto.KeyBytes (pointer types) so callers can call
// Zero() on them to scrub key material from memory — value types would
// silently copy the bytes on every assignment.
//
// refCount is the per-snapshot lease counter. Capture() increments it on
// the current snapshot; Release() decrements it on the same snapshot. The
// counter is a snap-local atomic, so Capture/Release can run from any
// goroutine without contending on the Manager mutex.
type KeySnapshot struct {
	KEK        *crypto.KeyBytes
	DEK        *crypto.KeyBytes
	KekVersion uint64
	DekVersion uint64
	CryptoMode crypto.Mode
	refCount   atomic.Int64
}

// Manager is the heart of the credential router's thread-safe key access.
//
// Concurrency model:
//   - A single sync.Mutex protects the current/previous pair. All reads,
//     writes, and atomic transitions (Swap, ClearPrevious, Capture) take
//     the mutex. The critical section is two lines (one Load, one Add),
//     so contention is negligible compared to the DB write each CRUD
//     performs after Capture.
//   - The pair is always published as a unit: any reader that sees the
//     new current also sees the previous pointing to the old current, so
//     there is no intermediate state and DrainCheck can trust previous
//     without re-validating against current.
//   - refCount is snap-local; Release is lock-free and decrements the
//     exact snap the caller captured, even if the Manager has already
//     published a new current. The snap pointer keeps the *KeySnapshot
//     alive in the heap until Release runs.
type Manager struct {
	mu       sync.Mutex
	current  *KeySnapshot
	previous *KeySnapshot
}

func NewManager() *Manager {
	return &Manager{}
}

func (m *Manager) Current() *KeySnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.current
}

func (m *Manager) Previous() *KeySnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.previous
}

func (m *Manager) HasCurrent() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.current != nil
}

func (m *Manager) HasPrevious() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.previous != nil
}

// Capture returns the current snapshot and increments its refCount under
// the Manager mutex. Every non-nil Capture MUST be paired with exactly one
// Release on the returned snapshot. Returns nil when no snapshot has been
// installed yet (callers must check before defer-Release).
func (m *Manager) Capture() *KeySnapshot {
	m.mu.Lock()
	snap := m.current
	if snap != nil {
		snap.refCount.Add(1)
	}
	m.mu.Unlock()
	return snap
}

// Release decrements the refCount of the snapshot previously returned by
// Capture. snap==nil is a no-op.
func (m *Manager) Release(snap *KeySnapshot) {
	if snap == nil {
		return
	}
	snap.refCount.Add(-1)
}

// Swap transitions to a new snapshot. The pair is published as a unit
// under the mutex, so any reader that observes the new current also
// observes previous pointing to the old current.
func (m *Manager) Swap(newSnap *KeySnapshot) *KeySnapshot {
	m.mu.Lock()
	old := m.current
	m.previous = old
	m.current = newSnap
	m.mu.Unlock()
	return old
}

// ClearPrevious drops the previous snapshot. Must only be called after
// DrainCheck has confirmed no inflight holders remain on the old snap.
func (m *Manager) ClearPrevious() {
	m.mu.Lock()
	m.previous = nil
	m.mu.Unlock()
}

// DrainCheck returns true when no pre-swap CRUDs are still holding the
// old snapshot. Rotation calls this from CompleteKEKRotation /
// CompleteDEKRotation, which run *after* Swap, so the snapshot we need to
// wait on is always m.previous. Before any Swap (or after ClearPrevious)
// m.previous is nil and DrainCheck returns true.
func (m *Manager) DrainCheck() bool {
	m.mu.Lock()
	prev := m.previous
	m.mu.Unlock()
	if prev == nil {
		return true
	}
	return prev.refCount.Load() == 0
}

// WaitInflightDrained blocks until DrainCheck returns true or context is cancelled.
// Polls every 50ms.
func (m *Manager) WaitInflightDrained(ctx context.Context) error {
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		if m.DrainCheck() {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// ZeroAll zeroes KEK and DEK bytes in both current and previous snapshots.
// Called on graceful shutdown to scrub key material from memory. Drains
// first so we never wipe a snapshot that an in-flight Capture still holds.
// Uses *crypto.KeyBytes.Zero() for explicit zeroisation: assignment alone
// would only drop the Go reference, not scrub the underlying bytes.
func (m *Manager) ZeroAll(ctx context.Context) error {
	if err := m.WaitInflightDrained(ctx); err != nil {
		return err
	}
	m.mu.Lock()
	cur, prev := m.current, m.previous
	m.mu.Unlock()
	if cur != nil {
		if cur.KEK != nil {
			cur.KEK.Zero()
		}
		if cur.DEK != nil {
			cur.DEK.Zero()
		}
	}
	if prev != nil {
		if prev.KEK != nil {
			prev.KEK.Zero()
		}
		if prev.DEK != nil {
			prev.DEK.Zero()
		}
	}
	return nil
}

// InstallDualSnap sets current=active and previous=pending under the
// mutex. Used at startup when both active and pending keys exist
// (mid-rotation crash recovery).
func (m *Manager) InstallDualSnap(active, pending *KeySnapshot) {
	m.mu.Lock()
	m.current = active
	m.previous = pending
	m.mu.Unlock()
}
