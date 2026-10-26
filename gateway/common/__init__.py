"""Stateless common helpers owned by the gateway package.

Only modules whose logic is pure (no shared process-global state, no config
file ownership) live here; they are the single source of truth and
``jiuwenswarm`` imports them from this package instead of keeping a second
copy. Stateful/shared modules (config, utils, auth, external_providers,
base_crypto, etcd, updater, ...) stay under ``jiuwenswarm.common`` and are
imported from there until the shared-common/package split lands.
"""
