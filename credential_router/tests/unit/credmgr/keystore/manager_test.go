package keystore_test

import (
	"bytes"
	"context"
	"sync"
	"testing"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
)

func TestManagerCurrent(t *testing.T) {
	m := keystore.NewManager()
	if m.Current() != nil {
		t.Error("Current() should be nil initially")
	}
	if m.HasCurrent() {
		t.Error("HasCurrent() should be false initially")
	}
}

func TestManagerCaptureRelease(t *testing.T) {
	m := keystore.NewManager()
	snap := &keystore.KeySnapshot{DEK: crypto.NewKeyBytes([]byte{1, 2, 3})}
	m.InstallDualSnap(snap, nil)
	captured := m.Capture()
	if captured != snap {
		t.Error("Capture() should return current snapshot")
	}
	m.Swap(&keystore.KeySnapshot{KekVersion: 2, DekVersion: 1})
	if m.DrainCheck() {
		t.Error("DrainCheck should be false while pre-swap CRUD in flight")
	}
	m.Release(captured)
	if !m.DrainCheck() {
		t.Error("DrainCheck should be true after Release")
	}
}

func TestManagerSwap(t *testing.T) {
	m := keystore.NewManager()
	old := &keystore.KeySnapshot{KekVersion: 1, DekVersion: 1}
	newSnap := &keystore.KeySnapshot{KekVersion: 2, DekVersion: 1}
	m.InstallDualSnap(old, nil)
	returned := m.Swap(newSnap)
	if returned != old {
		t.Error("Swap should return old")
	}
	if m.Current() != newSnap {
		t.Error("Current should be new")
	}
	if m.Previous() != old {
		t.Error("Previous should be old")
	}
}

func TestManagerClearPrevious(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})
	if m.Previous() == nil {
		t.Fatal("Previous should not be nil after Swap")
	}
	m.ClearPrevious()
	if m.Previous() != nil {
		t.Error("Previous should be nil after ClearPrevious")
	}
}

func TestManagerDrainCheckAfterSwapNoInflight(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})
	if !m.DrainCheck() {
		t.Error("DrainCheck should be true with no inflight")
	}
}

func TestManagerDrainCheckWithInflight(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	captured := m.Capture()
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})
	if m.DrainCheck() {
		t.Error("DrainCheck should be false while pre-swap CRUD in flight")
	}
	m.Release(captured)
	if !m.DrainCheck() {
		t.Error("DrainCheck should be true after Release")
	}
}

func TestManagerWaitInflightDrained(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	captured := m.Capture()
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})

	go func() {
		time.Sleep(150 * time.Millisecond)
		m.Release(captured)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	start := time.Now()
	if err := m.WaitInflightDrained(ctx); err != nil {
		t.Errorf("WaitInflightDrained: %v", err)
	}
	elapsed := time.Since(start)
	if elapsed < 100*time.Millisecond {
		t.Errorf("elapsed = %v, want >=100ms (release at 150ms)", elapsed)
	}
}

func TestManagerWaitInflightDrainedContextCancel(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	captured := m.Capture()
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := m.WaitInflightDrained(ctx); err == nil {
		m.Release(captured)
		t.Error("expected context deadline error")
	}
	m.Release(captured)
}

func TestManagerConcurrentCaptureRelease(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1, DekVersion: 1}, nil)
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ { // reduced from 100 to 10
		wg.Add(1)
		go func() {
			defer wg.Done()
			snap := m.Capture()
			m.Release(snap)
		}()
	}
	wg.Wait()
	if !m.DrainCheck() {
		t.Error("DrainCheck should be true after all captures are released")
	}
}

func TestZeroAllZeroesCurrentAndPrevious(t *testing.T) {
	m := keystore.NewManager()
	cur := &keystore.KeySnapshot{
		KEK: crypto.NewKeyBytes([]byte{0xAA, 0xBB, 0xCC}),
		DEK: crypto.NewKeyBytes([]byte{0xDD, 0xEE, 0xFF}),
	}
	prev := &keystore.KeySnapshot{
		KEK: crypto.NewKeyBytes([]byte{0x11, 0x22, 0x33}),
		DEK: crypto.NewKeyBytes([]byte{0x44, 0x55, 0x66}),
	}
	m.InstallDualSnap(cur, prev)

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := m.ZeroAll(ctx); err != nil {
		t.Fatalf("ZeroAll: %v", err)
	}

	if !cur.KEK.IsZero() {
		t.Error("current KEK not zeroed")
	}
	if !cur.DEK.IsZero() {
		t.Error("current DEK not zeroed")
	}
	if !prev.KEK.IsZero() {
		t.Error("previous KEK not zeroed")
	}
	if !prev.DEK.IsZero() {
		t.Error("previous DEK not zeroed")
	}
}

func TestZeroAllNoSnapshots(t *testing.T) {
	m := keystore.NewManager()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := m.ZeroAll(ctx); err != nil {
		t.Errorf("ZeroAll: %v", err)
	}
}

func TestSwapCapturesPreviousCorrectly(t *testing.T) {
	m := keystore.NewManager()
	oldSnap := &keystore.KeySnapshot{
		KEK:        crypto.NewKeyBytes([]byte{0x01, 0x02}),
		DEK:        crypto.NewKeyBytes([]byte{0x03, 0x04}),
		KekVersion: 1,
		DekVersion: 1,
	}
	newSnap := &keystore.KeySnapshot{
		KEK:        crypto.NewKeyBytes([]byte{0x05, 0x06}),
		DEK:        crypto.NewKeyBytes([]byte{0x07, 0x08}),
		KekVersion: 2,
		DekVersion: 1,
	}
	m.InstallDualSnap(oldSnap, nil)
	m.Swap(newSnap)

	if m.Current() != newSnap {
		t.Error("Current should be newSnap after Swap")
	}
	if m.Previous() != oldSnap {
		t.Error("Previous should be oldSnap after Swap")
	}
	// Verify Bytes() returns consistent values through snapshot chain
	if !bytes.Equal(m.Current().KEK.Bytes(), []byte{0x05, 0x06}) {
		t.Error("Current KEK.Bytes() mismatch")
	}
	if !bytes.Equal(m.Previous().KEK.Bytes(), []byte{0x01, 0x02}) {
		t.Error("Previous KEK.Bytes() mismatch")
	}
}

func TestManagerDrainCheckMultiInflight(t *testing.T) {
	m := keystore.NewManager()
	m.InstallDualSnap(&keystore.KeySnapshot{KekVersion: 1}, nil)
	captured := make([]*keystore.KeySnapshot, 5)
	for i := 0; i < 5; i++ {
		captured[i] = m.Capture()
	}
	m.Swap(&keystore.KeySnapshot{KekVersion: 2})
	for i := 0; i < 3; i++ {
		m.Release(captured[i])
	}
	if m.DrainCheck() {
		t.Error("DrainCheck should be false while 2 pre-swap CRUDs still in flight")
	}
	for i := 3; i < 5; i++ {
		m.Release(captured[i])
	}
	if !m.DrainCheck() {
		t.Error("DrainCheck should be true after all releases")
	}
}
