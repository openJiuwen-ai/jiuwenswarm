# TTSE native-runtime contract fixture

This test-only package exercises the real ZIP, file Rail loader, configuration
preflight, resource lifecycle and agent-core TTSERail. It is versioned so unit
tests run from a clean checkout without local resources or an installed plugin.

The Rail factory is a representative implementation of plugin_runtime v1.
Changes to that contract must update this fixture and its behavior tests together.
Passing these tests verifies the host contract; it does not verify an unversioned
production plugin, which must be checked against its own release artifact.

Tests copy this directory into their own tmp_path before editing or packaging it.
The production packager still defaults to jiuwenswarm/resources/plugins/ttse;
tests pass their fixture source explicitly. No fallback or skip is used.
