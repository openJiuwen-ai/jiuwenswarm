"""Gateway-side copies of stateless common helpers.

Only modules whose logic is pure (no shared process-global state, no config
file ownership) are vendored here. Stateful/shared modules (config, utils,
auth, external_providers, base_crypto, etcd, updater, ...) intentionally keep
importing ``jiuwenswarm.common`` until the shared-common/package split lands.
"""
