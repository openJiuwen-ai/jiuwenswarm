//go:build cgo && test

package keystore

import (
	"context"
	"time"
)

// Test-support accessors for the external white-box test suite in
// tests/unit/keystore. The `cgo && test` build constraint excludes this file
// from production builds (both non-cgo and cgo `go build ./...`) while
// leaving the symbols available during `go test ./tests/unit/...`.

// SetFaultHookForTesting installs the crash-injection hook consumed by
// invokeFault at rotation stage boundaries (see rotation.go for the supported
// stage list). It returns a restore function that reinstates the previous
// hook so tests can `defer restore()`. Passing nil disables injection.
func SetFaultHookForTesting(fn func(stage string)) func() {
	prev := faultHook
	faultHook = fn
	return func() { faultHook = prev }
}

// SetManagerForTesting replaces the Rotator's Manager, simulating a process
// restart where a fresh Manager is loaded from disk. Used by the
// crash-injection tests to exercise the recovery + reload flows.
func (r *Rotator) SetManagerForTesting(mgr *Manager) {
	r.manager = mgr
}

// LockRotationForTesting acquires the rotation mutex and returns a function
// that releases it. Tests hold the mutex to simulate an in-progress rotation
// and assert the ErrRotationInProgress guard.
func (r *Rotator) LockRotationForTesting() func() {
	r.adminRotationMu.Lock()
	return r.adminRotationMu.Unlock
}

// RunAutoRotateForTesting invokes the auto-rotate gate + single rotation
// cycle synchronously. Exposed so external tests can exercise the gate
// branches (fresh install, gate miss, rotation pending) without a ticker.
func (r *Rotator) RunAutoRotateForTesting(ctx context.Context, period time.Duration, drainTimeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) error {
	return r.runAutoRotate(ctx, period, drainTimeout, maxRowsPerTx, maxPhaseALoops)
}
