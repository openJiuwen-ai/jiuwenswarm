package platform

import (
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// ── Defaults (single source of truth) ──────────────────────────────────────
// Every default is defined exactly once here so Default() and Load()'s
// fallback never drift out of sync.
const (
	DefaultUpstreamTimeoutMs = 30000

	// Path defaults. Operator only configures the two directories below;
	// every derived path (DB file, S1/S2 locations) is computed from them.
	DefaultDataDir    = "./data"
	DefaultBackupDir  = "./backups"
	DefaultCryptoMode = "aes"

	DefaultRotationPeriod               = 30 * 24 * time.Hour
	DefaultRotationMaxPhaseALoops       = 100
	DefaultRotationDrainTimeout         = 5 * time.Minute
	DefaultRotationCompleteDrainTimeout = 30 * time.Second

	DefaultCacheMaxEntries   = 10000
	DefaultCacheTombstoneTTL = time.Hour
	DefaultCacheEntryTTL     = 10 * time.Minute

	DefaultAdminAddr           = "127.0.0.1:8081"
	DefaultAdminUserIDMaxLen   = 256
	DefaultAdminRealURLMaxLen  = 2048
	DefaultAdminKeyTagMaxLen   = 64
	DefaultAdminAPIKeyMaxLen   = 8192
	DefaultAdminAuthTypeMaxLen = 16

	DefaultServerBindAddress        = "127.0.0.1:8080"
	DefaultServerMaxResponseBytes   = 10 * 1024 * 1024
	DefaultServerMaxRequestBytes    = 10 * 1024 * 1024
	DefaultServerShutdownZeroBudget = 5 * time.Second
	DefaultServerReadHeaderTimeout  = 10 * time.Second
	DefaultServerShutdownTimeout    = 10 * time.Second
	DefaultServerIdleConnTimeout    = 90 * time.Second

	// DefaultSSRFDialCheck is false: the product ships as an appliance
	// where all inference is internal, and a default-on guard would block
	// legitimate proxy calls to the same-host services. Operators
	// exposing the proxy externally must opt in.
	DefaultSSRFDialCheck    = false
	DefaultSSRFCacheTTL     = 30 * time.Second
	DefaultSSRFTimeout      = 5 * time.Second

	DefaultBackupKeep       = 3
	DefaultBackupFilenameTpl = "backup-{type}-{ts}.db"

	DefaultBackupKeySnapshotEnabled     = true
	DefaultBackupKeySnapshotFilenameTpl = "key-snapshot-{ts}.bin"
	DefaultBackupKeySnapshotKeep        = 5

	DefaultRecoveryMaxWait = 5 * time.Minute
)

// ── Rotation config (KEK/DEK drain windows) ───────────────────────────────

type RotationConfig struct {
	Period               time.Duration `yaml:"period"`
	MaxPhaseALoops       int           `yaml:"max_phase_a_loops"`
	DrainTimeout         time.Duration `yaml:"drain_timeout"`
	CompleteDrainTimeout time.Duration `yaml:"complete_drain_timeout"`
	// MaxRowsPerTx intentionally absent — hardcoded in
	// internal/credmgr/keystore (see keystore.MaxRowsPerTx).
}

// ── Cache config (LRU + tombstone limits) ─────────────────────────────────

type CacheConfig struct {
	MaxEntries   int           `yaml:"max_entries"`
	TombstoneTTL time.Duration `yaml:"tombstone_ttl"`
	EntryTTL     time.Duration `yaml:"entry_ttl"`
}

// ── Admin config + validation limits ──────────────────────────────────────

type ValidationConfig struct {
	UserIDMaxLen   int `yaml:"user_id_max_len"`
	RealURLMaxLen  int `yaml:"real_url_max_len"`
	KeyTagMaxLen   int `yaml:"key_tag_max_len"`
	APIKeyMaxLen   int `yaml:"api_key_max_len"`
	AuthTypeMaxLen int `yaml:"auth_type_max_len"`
}

type AdminConfig struct {
	Addr       string           `yaml:"addr"`
	Validation ValidationConfig `yaml:"validation"`
}

// ── SSRF config (proxy outbound dial-check + admin host whitelist) ────────
// All SSRF policy in one block: the dialer's outbound check on the proxy
// path and the host whitelist enforced on the admin's real_url validation.
// Both layers use the same AllowedHosts list — defense in depth.

type SSRFConfig struct {
	DialCheck    bool          `yaml:"dial_check"`
	AllowedHosts []string      `yaml:"allowed_hosts"`
	CacheTTL     time.Duration `yaml:"cache_ttl"`
	Timeout      time.Duration `yaml:"timeout"`
}

// ── Proxy HTTP server config (port 8080) ──────────────────────────────────

type ServerConfig struct {
	BindAddress        string        `yaml:"bind_address"`
	ExternalAddress    string        `yaml:"external_address"`
	MaxResponseBytes   int64         `yaml:"max_response_bytes"`
	MaxRequestBytes    int64         `yaml:"max_request_bytes"`
	ShutdownZeroBudget time.Duration `yaml:"shutdown_zero_budget"`
	ReadHeaderTimeout  time.Duration `yaml:"read_header_timeout"`
	ShutdownTimeout    time.Duration `yaml:"shutdown_timeout"`
	IdleConnTimeout    time.Duration `yaml:"idle_conn_timeout"`
	LogFile            string        `yaml:"log_file"`
}

// ── Backup config (DB snapshots + key material snapshots) ─────────────────

type KeySnapshotConfig struct {
	Enabled     bool   `yaml:"enabled"`
	FilenameTpl string `yaml:"filename_template"`
	Keep        int    `yaml:"keep"`
}

type BackupConfig struct {
	// Path intentionally absent — uses top-level Config.BackupDir.
	// Keep applies to both KEK and DEK retention (same operator intent).
	Keep        int               `yaml:"keep"`
	FilenameTpl string            `yaml:"filename_template"`
	KeySnapshot KeySnapshotConfig `yaml:"key_snapshot"`
}

// ── Recovery config (max wait for crash recovery) ─────────────────────────

type RecoveryConfig struct {
	MaxWait time.Duration `yaml:"max_wait"`
}

// ── Top-level Config ───────────────────────────────────────────────────────

type Config struct {
	UpstreamTimeoutMs int    `yaml:"upstream_timeout_ms"`

	// Two operator-configured paths; everything else is derived. S1
	// (local shard) lives under DataDir/secrets; S2 is stored in the
	// SQLite key_metadata table, not on disk.
	DataDir    string `yaml:"data_dir"`
	BackupDir  string `yaml:"backup_dir"`
	CryptoMode string `yaml:"crypto_mode"`

	Rotation RotationConfig `yaml:"rotation"`
	Cache    CacheConfig    `yaml:"cache"`
	Admin    AdminConfig    `yaml:"admin"`
	SSRF     SSRFConfig     `yaml:"ssrf"`
	Server   ServerConfig   `yaml:"server"`
	Backup   BackupConfig   `yaml:"backup"`
	Recovery RecoveryConfig `yaml:"recovery"`
}

// ── Path accessors ─────────────────────────────────────────────────────────
// Operator only sets DataDir / BackupDir; the helpers below compute every
// concrete on-disk path so callers never assemble paths themselves.

// DBPath returns the path to the main credentials SQLite database.
func (c Config) DBPath() string {
	return filepath.Join(c.DataDir, "credentials.db")
}

// SecretsDir returns the directory holding the on-disk key material files
// (crypto_mode, s1.bin.{n}). Subdirectory of DataDir so a
// single mkdir creates both. S2 itself is stored in the SQLite
// key_metadata table, not in this directory.
func (c Config) SecretsDir() string {
	return filepath.Join(c.DataDir, "secrets")
}

// ── Default() ──────────────────────────────────────────────────────────────

func Default() Config {
	return Config{
		UpstreamTimeoutMs: DefaultUpstreamTimeoutMs,
		DataDir:    DefaultDataDir,
		BackupDir:  DefaultBackupDir,
		CryptoMode: DefaultCryptoMode,

		Rotation: RotationConfig{
			Period:               DefaultRotationPeriod,
			MaxPhaseALoops:       DefaultRotationMaxPhaseALoops,
			DrainTimeout:         DefaultRotationDrainTimeout,
			CompleteDrainTimeout: DefaultRotationCompleteDrainTimeout,
		},
		Cache: CacheConfig{
			MaxEntries:   DefaultCacheMaxEntries,
			TombstoneTTL: DefaultCacheTombstoneTTL,
			EntryTTL:     DefaultCacheEntryTTL,
		},
		Admin: AdminConfig{
			Addr: DefaultAdminAddr,
			Validation: ValidationConfig{
				UserIDMaxLen:   DefaultAdminUserIDMaxLen,
				RealURLMaxLen:  DefaultAdminRealURLMaxLen,
				KeyTagMaxLen:   DefaultAdminKeyTagMaxLen,
				APIKeyMaxLen:   DefaultAdminAPIKeyMaxLen,
				AuthTypeMaxLen: DefaultAdminAuthTypeMaxLen,
			},
		},
		SSRF: SSRFConfig{
			DialCheck: DefaultSSRFDialCheck,
			CacheTTL:  DefaultSSRFCacheTTL,
			Timeout:   DefaultSSRFTimeout,
		},
		Server: ServerConfig{
			BindAddress:        DefaultServerBindAddress,
			MaxResponseBytes:   DefaultServerMaxResponseBytes,
			MaxRequestBytes:    DefaultServerMaxRequestBytes,
			ShutdownZeroBudget: DefaultServerShutdownZeroBudget,
			ReadHeaderTimeout:  DefaultServerReadHeaderTimeout,
			ShutdownTimeout:    DefaultServerShutdownTimeout,
			IdleConnTimeout:    DefaultServerIdleConnTimeout,
		},
		Backup: BackupConfig{
			Keep:        DefaultBackupKeep,
			FilenameTpl: DefaultBackupFilenameTpl,
			KeySnapshot: KeySnapshotConfig{
				Enabled:     DefaultBackupKeySnapshotEnabled,
				FilenameTpl: DefaultBackupKeySnapshotFilenameTpl,
				Keep:        DefaultBackupKeySnapshotKeep,
			},
		},
		Recovery: RecoveryConfig{
			MaxWait: DefaultRecoveryMaxWait,
		},
	}
}

// ── Load() ─────────────────────────────────────────────────────────────────

func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		if err := cfg.Validate(); err != nil {
			return cfg, err
		}
		return cfg, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config: %w", err)
	}
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config: %w", err)
	}

	if cfg.UpstreamTimeoutMs <= 0 {
		cfg.UpstreamTimeoutMs = DefaultUpstreamTimeoutMs
	}
	if cfg.Server.BindAddress == "" {
		cfg.Server.BindAddress = DefaultServerBindAddress
	}

	// ── Path defaults ──
	if cfg.DataDir == "" {
		cfg.DataDir = DefaultDataDir
	}
	if cfg.BackupDir == "" {
		cfg.BackupDir = DefaultBackupDir
	}

	// ── Crypto mode defaults ──
	if cfg.CryptoMode == "" {
		cfg.CryptoMode = DefaultCryptoMode
	}

	// ── Rotation defaults ──
	if cfg.Rotation.Period <= 0 {
		cfg.Rotation.Period = DefaultRotationPeriod
	}
	if cfg.Rotation.MaxPhaseALoops <= 0 {
		cfg.Rotation.MaxPhaseALoops = DefaultRotationMaxPhaseALoops
	}
	if cfg.Rotation.DrainTimeout <= 0 {
		cfg.Rotation.DrainTimeout = DefaultRotationDrainTimeout
	}
	if cfg.Rotation.CompleteDrainTimeout <= 0 {
		cfg.Rotation.CompleteDrainTimeout = DefaultRotationCompleteDrainTimeout
	}

	// ── Cache defaults ──
	if cfg.Cache.MaxEntries <= 0 {
		cfg.Cache.MaxEntries = DefaultCacheMaxEntries
	}
	if cfg.Cache.TombstoneTTL <= 0 {
		cfg.Cache.TombstoneTTL = DefaultCacheTombstoneTTL
	}
	if cfg.Cache.EntryTTL <= 0 {
		cfg.Cache.EntryTTL = DefaultCacheEntryTTL
	}

	// ── Admin defaults ──
	if cfg.Admin.Addr == "" {
		cfg.Admin.Addr = DefaultAdminAddr
	}
	if cfg.Admin.Validation.UserIDMaxLen <= 0 {
		cfg.Admin.Validation.UserIDMaxLen = DefaultAdminUserIDMaxLen
	}
	if cfg.Admin.Validation.RealURLMaxLen <= 0 {
		cfg.Admin.Validation.RealURLMaxLen = DefaultAdminRealURLMaxLen
	}
	if cfg.Admin.Validation.KeyTagMaxLen <= 0 {
		cfg.Admin.Validation.KeyTagMaxLen = DefaultAdminKeyTagMaxLen
	}
	if cfg.Admin.Validation.APIKeyMaxLen <= 0 {
		cfg.Admin.Validation.APIKeyMaxLen = DefaultAdminAPIKeyMaxLen
	}
	if cfg.Admin.Validation.AuthTypeMaxLen <= 0 {
		cfg.Admin.Validation.AuthTypeMaxLen = DefaultAdminAuthTypeMaxLen
	}

	// ── Server defaults ──
	if cfg.Server.MaxResponseBytes <= 0 {
		cfg.Server.MaxResponseBytes = DefaultServerMaxResponseBytes
	}
	if cfg.Server.MaxRequestBytes <= 0 {
		cfg.Server.MaxRequestBytes = DefaultServerMaxRequestBytes
	}
	if cfg.Server.ShutdownZeroBudget <= 0 {
		cfg.Server.ShutdownZeroBudget = DefaultServerShutdownZeroBudget
	}
	if cfg.Server.ReadHeaderTimeout <= 0 {
		cfg.Server.ReadHeaderTimeout = DefaultServerReadHeaderTimeout
	}
	if cfg.Server.ShutdownTimeout <= 0 {
		cfg.Server.ShutdownTimeout = DefaultServerShutdownTimeout
	}
	if cfg.Server.IdleConnTimeout <= 0 {
		cfg.Server.IdleConnTimeout = DefaultServerIdleConnTimeout
	}
	// ── SSRF defaults (DialCheck is bool; false IS the default) ──
	if cfg.SSRF.CacheTTL <= 0 {
		cfg.SSRF.CacheTTL = DefaultSSRFCacheTTL
	}
	if cfg.SSRF.Timeout <= 0 {
		cfg.SSRF.Timeout = DefaultSSRFTimeout
	}

	// ── Backup defaults (Path uses top-level BackupDir) ──
	if cfg.Backup.Keep <= 0 {
		cfg.Backup.Keep = DefaultBackupKeep
	}
	if cfg.Backup.FilenameTpl == "" {
		cfg.Backup.FilenameTpl = DefaultBackupFilenameTpl
	}
	if cfg.Backup.KeySnapshot.FilenameTpl == "" {
		cfg.Backup.KeySnapshot.FilenameTpl = DefaultBackupKeySnapshotFilenameTpl
	}
	if cfg.Backup.KeySnapshot.Keep <= 0 {
		cfg.Backup.KeySnapshot.Keep = DefaultBackupKeySnapshotKeep
	}

	// ── Recovery defaults ──
	if cfg.Recovery.MaxWait <= 0 {
		cfg.Recovery.MaxWait = DefaultRecoveryMaxWait
	}

	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	return cfg, nil
}

// ── Validate() ─────────────────────────────────────────────────────────────

func (c Config) Validate() error {
	if c.Server.BindAddress != "" {
		if _, _, err := net.SplitHostPort(c.Server.BindAddress); err != nil {
			return fmt.Errorf("invalid server.bind_address: %w", err)
		}
		if host, _, splitErr := net.SplitHostPort(c.Server.BindAddress); splitErr == nil && (host == "0.0.0.0" || host == "::") {
			if strings.TrimSpace(c.Server.ExternalAddress) == "" {
				return fmt.Errorf("server.bind_address is wildcard (%q); server.external_address must be set so the admin API can return a usable proxy URL to clients", host)
			}
			if _, err := url.Parse(c.Server.ExternalAddress); err != nil || !strings.HasPrefix(c.Server.ExternalAddress, "http://") && !strings.HasPrefix(c.Server.ExternalAddress, "https://") {
				return fmt.Errorf("server.external_address %q must be a http(s) URL", c.Server.ExternalAddress)
			}
		}
	}
	if strings.TrimSpace(c.DataDir) == "" {
		return fmt.Errorf("data_dir is required")
	}
	if strings.TrimSpace(c.BackupDir) == "" {
		return fmt.Errorf("backup_dir is required")
	}
	switch strings.ToLower(c.CryptoMode) {
	case "aes", "sm4":
	default:
		return fmt.Errorf("crypto_mode must be aes or sm4, got %q", c.CryptoMode)
	}
	if strings.TrimSpace(c.Admin.Addr) == "" {
		return fmt.Errorf("admin.addr is required")
	}
	if _, _, err := net.SplitHostPort(c.Admin.Addr); err != nil {
		return fmt.Errorf("admin.addr %q is not a valid host:port: %w", c.Admin.Addr, err)
	}

	// Numeric / duration fields. Two classes:
	//
	//   mustBePositive* — reject 0 and negative. Use for fields where 0 is
	//     meaningless (loop counters, byte caps, keep counts, mandatory
	//     timeouts).
	//
	//   nonNegative* — reject only negative. Use for fields where 0 has a
	//     documented "off" / "no limit" semantic (intervals can disable
	//     auto-rotation; TTLs can disable caching; server.idle_conn_timeout
	//     == 0 is documented by net/http as "no idle limit"; ssrf.cache_ttl
	//     == 0 disables caching — see CONFIG.md).
	//
	// Load() substitutes <=0 with defaults, so missing YAML keys do not
	// error. Validate() catches errors in code paths that bypass Load() or
	// hand-construct a Config with a non-default zero.
	if err := nonNegative("upstream_timeout_ms", c.UpstreamTimeoutMs); err != nil {
		return err
	}
	if err := nonNegativeDuration("rotation.period", c.Rotation.Period); err != nil {
		return err
	}
	if err := mustBePositive("rotation.max_phase_a_loops", c.Rotation.MaxPhaseALoops); err != nil {
		return err
	}
	if err := nonNegativeDuration("rotation.drain_timeout", c.Rotation.DrainTimeout); err != nil {
		return err
	}
	if err := nonNegativeDuration("rotation.complete_drain_timeout", c.Rotation.CompleteDrainTimeout); err != nil {
		return err
	}
	if err := mustBePositive("cache.max_entries", c.Cache.MaxEntries); err != nil {
		return err
	}
	if err := nonNegativeDuration("cache.tombstone_ttl", c.Cache.TombstoneTTL); err != nil {
		return err
	}
	if err := nonNegativeDuration("cache.entry_ttl", c.Cache.EntryTTL); err != nil {
		return err
	}
	vm := c.Admin.Validation
	if err := mustBePositive("admin.validation.user_id_max_len", vm.UserIDMaxLen); err != nil {
		return err
	}
	if err := mustBePositive("admin.validation.real_url_max_len", vm.RealURLMaxLen); err != nil {
		return err
	}
	if err := mustBePositive("admin.validation.key_tag_max_len", vm.KeyTagMaxLen); err != nil {
		return err
	}
	if err := mustBePositive("admin.validation.api_key_max_len", vm.APIKeyMaxLen); err != nil {
		return err
	}
	if err := mustBePositive("admin.validation.auth_type_max_len", vm.AuthTypeMaxLen); err != nil {
		return err
	}
	if err := mustBePositiveInt64("server.max_response_bytes", c.Server.MaxResponseBytes); err != nil {
		return err
	}
	if err := mustBePositiveInt64("server.max_request_bytes", c.Server.MaxRequestBytes); err != nil {
		return err
	}
	if err := nonNegativeDuration("server.shutdown_zero_budget", c.Server.ShutdownZeroBudget); err != nil {
		return err
	}
	if err := mustBePositiveDuration("server.read_header_timeout", c.Server.ReadHeaderTimeout); err != nil {
		return err
	}
	if err := mustBePositiveDuration("server.shutdown_timeout", c.Server.ShutdownTimeout); err != nil {
		return err
	}
	if err := nonNegativeDuration("server.idle_conn_timeout", c.Server.IdleConnTimeout); err != nil {
		return err
	}
	if err := nonNegativeDuration("ssrf.cache_ttl", c.SSRF.CacheTTL); err != nil {
		return err
	}
	if err := mustBePositiveDuration("ssrf.timeout", c.SSRF.Timeout); err != nil {
		return err
	}
	if err := mustBePositive("backup.keep", c.Backup.Keep); err != nil {
		return err
	}
	if err := mustBePositive("backup.key_snapshot.keep", c.Backup.KeySnapshot.Keep); err != nil {
		return err
	}
	if err := mustBePositiveDuration("recovery.max_wait", c.Recovery.MaxWait); err != nil {
		return err
	}
	return nil
}

// nonNegative* helpers — see Validate() docstring for the 0-is-allowed
// vs must-be-positive split.
func nonNegative(field string, v int) error {
	if v < 0 {
		return fmt.Errorf("%s must be >= 0, got %d", field, v)
	}
	return nil
}

func nonNegativeInt64(field string, v int64) error {
	if v < 0 {
		return fmt.Errorf("%s must be >= 0, got %d", field, v)
	}
	return nil
}

func nonNegativeDuration(field string, v time.Duration) error {
	if v < 0 {
		return fmt.Errorf("%s must be >= 0, got %v", field, v)
	}
	return nil
}

// mustBePositive* helpers reject 0 and negative values. Use for fields
// where zero is meaningless (loop counters, byte caps, keep counts,
// mandatory timeouts).
func mustBePositive(field string, v int) error {
	if v <= 0 {
		return fmt.Errorf("%s must be > 0, got %d", field, v)
	}
	return nil
}

func mustBePositiveInt64(field string, v int64) error {
	if v <= 0 {
		return fmt.Errorf("%s must be > 0, got %d", field, v)
	}
	return nil
}

func mustBePositiveDuration(field string, v time.Duration) error {
	if v <= 0 {
		return fmt.Errorf("%s must be > 0, got %v", field, v)
	}
	return nil
}

// ── Accessors ──────────────────────────────────────────────────────────────

func (c Config) UpstreamTimeout() time.Duration {
	return time.Duration(c.UpstreamTimeoutMs) * time.Millisecond
}