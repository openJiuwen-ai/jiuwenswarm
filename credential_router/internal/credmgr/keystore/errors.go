package keystore

import "errors"

var (
	// ErrShardLength is returned when a shard is not 32 bytes.
	ErrShardLength = errors.New("keystore: shard must be 32 bytes")

	// ErrStartupRefused is returned by LoadFromDir when startup cannot proceed
	// due to a configuration error (missing files, mode mismatch, wrong sizes).
	// Caller must fix the config and restart. Maps to exit code 78 (FATAL).
	ErrStartupRefused = errors.New("keystore: startup refused")

	// ErrStartupFatal is returned when in-memory key state is inconsistent
	// (e.g. probeDecrypt fails — derived KEK cannot decrypt stored DEK).
	// Indicates data corruption or wrong key file. Caller must exit immediately
	// without attempting further operations. Maps to exit code 78 (FATAL).
	ErrStartupFatal = errors.New("keystore: startup fatal — data corruption detected")

	// ErrNotInitialized is returned by LoadFromDir when the DB has no
	// key_metadata row. This is the normal "fresh install" signal — the
	// caller (main.go::bootstrapKeystore) branches into SelfInit based
	// on this. Distinct from ErrStartupRefused (corruption / misconfig
	// where DB has data but files are inconsistent).
	ErrNotInitialized = errors.New("keystore: not initialized")
)
