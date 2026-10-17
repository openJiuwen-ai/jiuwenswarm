//go:build instrumented

package main

import (
	"log/slog"

	"credential_router/internal/platform"
)

// init activates the pprof HTTP endpoint. This file is only compiled when the
// binary is built with -tags instrumented; production builds do not import
// internal/platform/pprof and carry no profiling code.
//
// slog.Default() at init time is the text handler; run() later swaps in the
// JSON handler. The handler reference is captured by platform.Start, so pprof
// logs stay in text format even after the swap. This is acceptable because
// pprof is a debugging-only feature used during TPCC concurrency profiling.
func init() {
	_ = platform.Start(slog.Default())
}