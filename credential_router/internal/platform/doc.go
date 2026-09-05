// Package platform contains the cross-cutting infrastructure for the
// credential-router binary:
//
//   - errs.go:          central error envelope (Code, New, Wrap, CodeOf, sentinels)
//   - config.go:        configuration types and YAML loader
//   - singleinstance.go: process-level flock guard
//   - pprof.go:         optional pprof HTTP endpoint (-tags instrumented)
//
// Each concern lives in its own file for discoverability; the package is
// intentionally monolithic because every other package in the binary
// depends on at least one of these.
package platform