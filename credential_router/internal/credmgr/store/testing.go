//go:build cgo && test

package store

import "context"

// SetReencryptPhaseHookForTesting installs a hook that runs between Phase A's
// SELECT and per-row UPDATE. It returns a restore function so tests can defer
// it back to nil. Passing nil disables the hook.
func SetReencryptPhaseHookForTesting(fn func(ctx context.Context)) func() {
	prev := reencryptPhaseHook
	reencryptPhaseHook = fn
	return func() { reencryptPhaseHook = prev }
}
