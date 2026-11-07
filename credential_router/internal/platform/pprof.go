//go:build instrumented

// Package pprof optionally exposes the Go runtime pprof HTTP endpoint.
//
// The entire package is gated behind `-tags instrumented`: in default
// builds the file is not compiled and the binary carries no pprof
// listener, runtime profiling fractions, or import path to this package.
// The companion no-op in production binaries lives one layer up — see
// cmd/credential-router/main.go (production builds have no profiling code
// at all; only `-tags instrumented` builds include the entry point).
//
// In instrumented builds, Start() unconditionally enables
// runtime.SetMutexProfileFraction and runtime.SetBlockProfileRate (the
// overhead is acceptable since this build tag never ships in production).
// When PPROF_ADDR is set in the environment, it additionally serves
// /debug/pprof/* on that address in a background goroutine.
//
// This package exists solely for the TPCC concurrency harness
// (tests/concurrency_tpcc_test.go and the
// //go:build cgo && instrumented-gated
// tests/concurrency_tpcc_instrumented_test.go). Production deployments do
// not use it.
package platform

import (
	"log/slog"
	"net/http"
	"os"
	"runtime"

	// Registers /debug/pprof/* on http.DefaultServeMux.
	_ "net/http/pprof"
)

// Start enables runtime profiling fractions and, when PPROF_ADDR is set in the
// environment, serves /debug/pprof/* on that address in a background goroutine.
// A nil Start return value means "nothing to do" (PPROF_ADDR unset) or
// "server already listening" (idempotent for the calling binary's lifetime).
func Start(logger *slog.Logger) error {
	if logger == nil {
		logger = slog.Default()
	}
	runtime.SetMutexProfileFraction(5)
	runtime.SetBlockProfileRate(1)

	addr := os.Getenv("PPROF_ADDR")
	if addr == "" {
		logger.Debug("pprof disabled", "reason", "PPROF_ADDR unset")
		return nil
	}

	go func() {
		logger.Info("pprof server starting", "addr", addr)
		if err := http.ListenAndServe(addr, nil); err != nil {
			logger.Error("pprof server failed", "addr", addr, "error", err)
		}
	}()
	return nil
}
