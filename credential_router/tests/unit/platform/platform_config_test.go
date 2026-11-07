package platform_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"credential_router/internal/platform"
)

func TestDefault(t *testing.T) {
	cfg := platform.Default()

	// Path defaults
	if cfg.DataDir != "./data" {
		t.Fatalf("data_dir default: got %q, want %q", cfg.DataDir, "./data")
	}
	if cfg.BackupDir != "./backups" {
		t.Fatalf("backup_dir default: got %q, want %q", cfg.BackupDir, "./backups")
	}

	// Listen defaults
	if cfg.Server.BindAddress != "127.0.0.1:8080" {
		t.Fatalf("server.bind_address default: got %q, want %q", cfg.Server.BindAddress, "127.0.0.1:8080")
	}

	// Rotation defaults
	if cfg.Rotation.Period != 30*24*time.Hour {
		t.Fatalf("rotation.period default: got %v", cfg.Rotation.Period)
	}
	if cfg.Rotation.MaxPhaseALoops != 100 {
		t.Fatalf("rotation.max_phase_a_loops default: got %d", cfg.Rotation.MaxPhaseALoops)
	}
	if cfg.Rotation.DrainTimeout != 5*time.Minute {
		t.Fatalf("rotation.drain_timeout default: got %v", cfg.Rotation.DrainTimeout)
	}
	if cfg.Rotation.CompleteDrainTimeout != 30*time.Second {
		t.Fatalf("rotation.complete_drain_timeout default: got %v", cfg.Rotation.CompleteDrainTimeout)
	}

	// Cache defaults
	if cfg.Cache.MaxEntries != 10000 {
		t.Fatalf("cache.max_entries default: got %d", cfg.Cache.MaxEntries)
	}
	if cfg.Cache.TombstoneTTL != time.Hour {
		t.Fatalf("cache.tombstone_ttl default: got %v", cfg.Cache.TombstoneTTL)
	}
	if cfg.Cache.EntryTTL != 10*time.Minute {
		t.Fatalf("cache.entry_ttl default: got %v", cfg.Cache.EntryTTL)
	}

	// Admin defaults
	if cfg.Admin.Addr != "127.0.0.1:8081" {
		t.Fatalf("admin.addr default: got %q", cfg.Admin.Addr)
	}
	if cfg.Admin.Validation.UserIDMaxLen != 256 {
		t.Fatalf("admin.validation.user_id_max_len default: got %d", cfg.Admin.Validation.UserIDMaxLen)
	}
	if cfg.Admin.Validation.RealURLMaxLen != 2048 {
		t.Fatalf("admin.validation.api_base_max_len default: got %d", cfg.Admin.Validation.RealURLMaxLen)
	}
	if cfg.Admin.Validation.KeyTagMaxLen != 64 {
		t.Fatalf("admin.validation.key_tag_max_len default: got %d", cfg.Admin.Validation.KeyTagMaxLen)
	}
	if cfg.Admin.Validation.APIKeyMaxLen != 8192 {
		t.Fatalf("admin.validation.api_key_max_len default: got %d", cfg.Admin.Validation.APIKeyMaxLen)
	}
	if cfg.Admin.Validation.AuthTypeMaxLen != 16 {
		t.Fatalf("admin.validation.auth_type_max_len default: got %d", cfg.Admin.Validation.AuthTypeMaxLen)
	}
	if cfg.SSRF.AllowedHosts != nil {
		t.Fatalf("ssrf.allowed_hosts default: got %v, want nil (empty = no whitelist)", cfg.SSRF.AllowedHosts)
	}
	if cfg.SSRF.DialCheck != false {
		t.Fatalf("ssrf.dial_check default: got %v, want false (appliance default)", cfg.SSRF.DialCheck)
	}
	if cfg.SSRF.CacheTTL != 30*time.Second {
		t.Fatalf("ssrf.cache_ttl default: got %v, want 30s", cfg.SSRF.CacheTTL)
	}
	if cfg.SSRF.Timeout != 5*time.Second {
		t.Fatalf("ssrf.timeout default: got %v, want 5s", cfg.SSRF.Timeout)
	}

	// Server defaults
	if cfg.Server.MaxResponseBytes != 10485760 {
		t.Fatalf("server.max_response_bytes default: got %d", cfg.Server.MaxResponseBytes)
	}
	if cfg.Server.MaxRequestBytes != 10485760 {
		t.Fatalf("server.max_request_bytes default: got %d", cfg.Server.MaxRequestBytes)
	}
	if cfg.Server.ShutdownZeroBudget != 5*time.Second {
		t.Fatalf("server.shutdown_zero_budget default: got %v, want 5s", cfg.Server.ShutdownZeroBudget)
	}
	if cfg.Server.ReadHeaderTimeout != 10*time.Second {
		t.Fatalf("server.read_header_timeout default: got %v, want 10s", cfg.Server.ReadHeaderTimeout)
	}
	if cfg.Server.ShutdownTimeout != 10*time.Second {
		t.Fatalf("server.shutdown_timeout default: got %v, want 10s", cfg.Server.ShutdownTimeout)
	}
	if cfg.Server.IdleConnTimeout != 90*time.Second {
		t.Fatalf("server.idle_conn_timeout default: got %v, want 90s", cfg.Server.IdleConnTimeout)
	}
	if cfg.SSRF.DialCheck != false {
		t.Fatalf("ssrf.dial_check default: got %v, want false (appliance default)", cfg.SSRF.DialCheck)
	}
	if cfg.SSRF.CacheTTL != 30*time.Second {
		t.Fatalf("ssrf.cache_ttl default: got %v, want 30s", cfg.SSRF.CacheTTL)
	}
	if cfg.SSRF.Timeout != 5*time.Second {
		t.Fatalf("server.timeout default: got %v, want 5s", cfg.SSRF.Timeout)
	}

	// Backup defaults
	if cfg.Backup.Keep != 3 {
		t.Fatalf("backup.keep default: got %d", cfg.Backup.Keep)
	}
	if cfg.Backup.Keep != 3 {
		t.Fatalf("backup.keep default: got %d", cfg.Backup.Keep)
	}
	if cfg.Backup.FilenameTpl != "backup-{type}-{ts}.db" {
		t.Fatalf("backup.filename_template default: got %q", cfg.Backup.FilenameTpl)
	}
	if cfg.Backup.KeySnapshot.Enabled != true {
		t.Fatalf("backup.key_snapshot.enabled default: got %v", cfg.Backup.KeySnapshot.Enabled)
	}
	if cfg.Backup.KeySnapshot.FilenameTpl != "key-snapshot-{ts}.bin" {
		t.Fatalf("backup.key_snapshot.filename_template default: got %q", cfg.Backup.KeySnapshot.FilenameTpl)
	}
	if cfg.Backup.KeySnapshot.Keep != 5 {
		t.Fatalf("backup.key_snapshot.keep default: got %d", cfg.Backup.KeySnapshot.Keep)
	}

	// Recovery defaults
	if cfg.Recovery.MaxWait != 5*time.Minute {
		t.Fatalf("recovery.max_wait default: got %v", cfg.Recovery.MaxWait)
	}
}

func TestPathHelpers(t *testing.T) {
	cfg := platform.Config{DataDir: "/var/lib/cr", BackupDir: "/var/backups/cr"}
	if got, want := cfg.DBPath(), "/var/lib/cr/credentials.db"; got != want {
		t.Errorf("DBPath() = %q, want %q", got, want)
	}
	if got, want := cfg.SecretsDir(), "/var/lib/cr/secrets"; got != want {
		t.Errorf("SecretsDir() = %q, want %q", got, want)
	}
}

func TestLoadFullYAML(t *testing.T) {
	content := `
server:
  bind_address: 0.0.0.0:9090
  external_address: http://router.example.com:9090
  max_response_bytes: 5242880
  shutdown_zero_budget: 3s
  read_header_timeout: 5s
  shutdown_timeout: 5s
  idle_conn_timeout: 60s

ssrf:
  dial_check: true
  cache_ttl: 5s
  timeout: 3s
  allowed_hosts:
    - api.example.com
    - "*.foo.com"
upstream_timeout_ms: 5000

data_dir: /custom/data
backup_dir: /custom/backups

rotation:
  period: 12h
  max_phase_a_loops: 50
  drain_timeout: 3m
  complete_drain_timeout: 15s

cache:
  max_entries: 5000
  tombstone_ttl: 30m
  entry_ttl: 5m

admin:
  addr: 10.0.0.1:9091
  validation:
    user_id_max_len: 128
    real_url_max_len: 1024
    key_tag_max_len: 32
    api_key_max_len: 4096
    auth_type_max_len: 8

backup:
  keep: 5
  filename_template: "custom-{type}-{ts}.bak"
  key_snapshot:
    enabled: false
    filename_template: "custom-key-{ts}.bin"
    keep: 10

recovery:
  max_wait: 10m
`

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := platform.Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}

	// Path
	if cfg.DataDir != "/custom/data" {
		t.Errorf("data_dir = %q", cfg.DataDir)
	}
	if cfg.BackupDir != "/custom/backups" {
		t.Errorf("backup_dir = %q", cfg.BackupDir)
	}

	// Listen
	if cfg.Server.BindAddress != "0.0.0.0:9090" {
		t.Errorf("server.bind_address = %q, want %q", cfg.Server.BindAddress, "0.0.0.0:9090")
	}
	if cfg.UpstreamTimeoutMs != 5000 {
		t.Errorf("upstream_timeout_ms = %d, want 5000", cfg.UpstreamTimeoutMs)
	}

	// Rotation
	if cfg.Rotation.Period != 12*time.Hour {
		t.Errorf("rotation.period = %v", cfg.Rotation.Period)
	}
	if cfg.Rotation.MaxPhaseALoops != 50 {
		t.Errorf("rotation.max_phase_a_loops = %d", cfg.Rotation.MaxPhaseALoops)
	}
	if cfg.Rotation.DrainTimeout != 3*time.Minute {
		t.Errorf("rotation.drain_timeout = %v, want 3m", cfg.Rotation.DrainTimeout)
	}
	if cfg.Rotation.CompleteDrainTimeout != 15*time.Second {
		t.Errorf("rotation.complete_drain_timeout = %v, want 15s", cfg.Rotation.CompleteDrainTimeout)
	}

	// Cache
	if cfg.Cache.MaxEntries != 5000 {
		t.Errorf("cache.max_entries = %d", cfg.Cache.MaxEntries)
	}
	if cfg.Cache.TombstoneTTL != 30*time.Minute {
		t.Errorf("cache.tombstone_ttl = %v", cfg.Cache.TombstoneTTL)
	}
	if cfg.Cache.EntryTTL != 5*time.Minute {
		t.Errorf("cache.entry_ttl = %v, want 5m", cfg.Cache.EntryTTL)
	}

	// Admin
	if cfg.Admin.Addr != "10.0.0.1:9091" {
		t.Errorf("admin.addr = %q", cfg.Admin.Addr)
	}
	if cfg.Admin.Validation.UserIDMaxLen != 128 {
		t.Errorf("admin.validation.user_id_max_len = %d", cfg.Admin.Validation.UserIDMaxLen)
	}
	if cfg.Admin.Validation.RealURLMaxLen != 1024 {
		t.Errorf("admin.validation.api_base_max_len = %d, want 1024", cfg.Admin.Validation.RealURLMaxLen)
	}
	if cfg.Admin.Validation.KeyTagMaxLen != 32 {
		t.Errorf("admin.validation.key_tag_max_len = %d, want 32", cfg.Admin.Validation.KeyTagMaxLen)
	}
	if cfg.Admin.Validation.APIKeyMaxLen != 4096 {
		t.Errorf("admin.validation.api_key_max_len = %d, want 4096", cfg.Admin.Validation.APIKeyMaxLen)
	}
	if cfg.Admin.Validation.AuthTypeMaxLen != 8 {
		t.Errorf("admin.validation.auth_type_max_len = %d, want 8", cfg.Admin.Validation.AuthTypeMaxLen)
	}
	if len(cfg.SSRF.AllowedHosts) != 2 ||
		cfg.SSRF.AllowedHosts[0] != "api.example.com" ||
		cfg.SSRF.AllowedHosts[1] != "*.foo.com" {
		t.Errorf("ssrf.allowed_hosts = %v, want [api.example.com *.foo.com]", cfg.SSRF.AllowedHosts)
	}

	// Server
	if cfg.Server.MaxResponseBytes != 5242880 {
		t.Errorf("server.max_response_bytes = %d", cfg.Server.MaxResponseBytes)
	}
	if cfg.Server.MaxRequestBytes != 10485760 {
		t.Errorf("server.max_request_bytes = %d, want default 10485760 (not set in YAML)", cfg.Server.MaxRequestBytes)
	}
	if cfg.Server.ShutdownZeroBudget != 3*time.Second {
		t.Errorf("server.shutdown_zero_budget = %v, want 3s", cfg.Server.ShutdownZeroBudget)
	}
	if cfg.Server.ReadHeaderTimeout != 5*time.Second {
		t.Errorf("server.read_header_timeout = %v, want 5s", cfg.Server.ReadHeaderTimeout)
	}
	if cfg.Server.ShutdownTimeout != 5*time.Second {
		t.Errorf("server.shutdown_timeout = %v, want 5s", cfg.Server.ShutdownTimeout)
	}
	if cfg.Server.IdleConnTimeout != 60*time.Second {
		t.Errorf("server.idle_conn_timeout = %v, want 60s", cfg.Server.IdleConnTimeout)
	}
	if cfg.SSRF.DialCheck != true {
		t.Errorf("ssrf.dial_check = %v, want true (set in YAML)", cfg.SSRF.DialCheck)
	}
	if cfg.SSRF.CacheTTL != 5*time.Second {
		t.Errorf("ssrf.cache_ttl = %v, want 5s", cfg.SSRF.CacheTTL)
	}
	if cfg.SSRF.Timeout != 3*time.Second {
		t.Errorf("ssrf.timeout = %v, want 3s", cfg.SSRF.Timeout)
	}

	// Backup
	if cfg.Backup.Keep != 5 {
		t.Errorf("backup.keep = %d", cfg.Backup.Keep)
	}
	if cfg.Backup.FilenameTpl != "custom-{type}-{ts}.bak" {
		t.Errorf("backup.filename_template = %q", cfg.Backup.FilenameTpl)
	}
	if cfg.Backup.KeySnapshot.Enabled != false {
		t.Errorf("backup.key_snapshot.enabled = %v", cfg.Backup.KeySnapshot.Enabled)
	}
	if cfg.Backup.KeySnapshot.FilenameTpl != "custom-key-{ts}.bin" {
		t.Errorf("backup.key_snapshot.filename_template = %q", cfg.Backup.KeySnapshot.FilenameTpl)
	}
	if cfg.Backup.KeySnapshot.Keep != 10 {
		t.Errorf("backup.key_snapshot.keep = %d", cfg.Backup.KeySnapshot.Keep)
	}

	// Recovery
	if cfg.Recovery.MaxWait != 10*time.Minute {
		t.Errorf("recovery.max_wait = %v", cfg.Recovery.MaxWait)
	}
}

func TestLoadEmptyYAML(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte("{}\n"), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	// Empty YAML is valid — all fields get defaults
	cfg, err := platform.Load(path)
	if err != nil {
		t.Fatalf("load empty config: %v", err)
	}
	if cfg.DataDir != "./data" {
		t.Errorf("data_dir default = %q", cfg.DataDir)
	}
	if cfg.BackupDir != "./backups" {
		t.Errorf("backup_dir default = %q", cfg.BackupDir)
	}
	if cfg.Admin.Addr != "127.0.0.1:8081" {
		t.Errorf("admin.addr default = %q", cfg.Admin.Addr)
	}
	if cfg.Rotation.Period != 30*24*time.Hour {
		t.Errorf("rotation.period default = %v", cfg.Rotation.Period)
	}
}

func TestLoadDefaultsApplied(t *testing.T) {
	// Minimal valid YAML — only set required fields, rest should get defaults
	content := `
data_dir: /var/lib/cr
backup_dir: /var/backups/cr
admin:
  addr: 127.0.0.1:8081
`

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := platform.Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}

	// Explicitly set
	if cfg.DataDir != "/var/lib/cr" {
		t.Errorf("data_dir = %q", cfg.DataDir)
	}
	if cfg.BackupDir != "/var/backups/cr" {
		t.Errorf("backup_dir = %q", cfg.BackupDir)
	}

	// Defaults applied
	if cfg.Rotation.Period != 30*24*time.Hour {
		t.Errorf("rotation.period default = %v", cfg.Rotation.Period)
	}
	if cfg.Rotation.MaxPhaseALoops != 100 {
		t.Errorf("rotation.max_phase_a_loops default = %d", cfg.Rotation.MaxPhaseALoops)
	}
	if cfg.Cache.MaxEntries != 10000 {
		t.Errorf("cache.max_entries default = %d", cfg.Cache.MaxEntries)
	}
	if cfg.Cache.TombstoneTTL != time.Hour {
		t.Errorf("cache.tombstone_ttl default = %v", cfg.Cache.TombstoneTTL)
	}
	if cfg.Server.MaxResponseBytes != 10485760 {
		t.Errorf("server.max_response_bytes default = %d", cfg.Server.MaxResponseBytes)
	}
	if cfg.Server.MaxRequestBytes != 10485760 {
		t.Errorf("server.max_request_bytes default = %d", cfg.Server.MaxRequestBytes)
	}
	if cfg.SSRF.DialCheck != false {
		t.Errorf("ssrf.dial_check default = %v, want false", cfg.SSRF.DialCheck)
	}
	if cfg.SSRF.CacheTTL != 30*time.Second {
		t.Errorf("ssrf.cache_ttl default = %v, want 30s", cfg.SSRF.CacheTTL)
	}
	if cfg.SSRF.Timeout != 5*time.Second {
		t.Errorf("server.timeout default = %v, want 5s", cfg.SSRF.Timeout)
	}
	if cfg.Backup.Keep != 3 {
		t.Errorf("backup.keep default = %d", cfg.Backup.Keep)
	}
	if cfg.Backup.Keep != 3 {
		t.Errorf("backup.keep default = %d", cfg.Backup.Keep)
	}
	if cfg.Backup.KeySnapshot.Keep != 5 {
		t.Errorf("backup.key_snapshot.keep default = %d", cfg.Backup.KeySnapshot.Keep)
	}
	if cfg.Recovery.MaxWait != 5*time.Minute {
		t.Errorf("recovery.max_wait default = %v", cfg.Recovery.MaxWait)
	}
	if cfg.Admin.Validation.UserIDMaxLen != 256 {
		t.Errorf("admin.validation.user_id_max_len default = %d", cfg.Admin.Validation.UserIDMaxLen)
	}
}

func TestLoadPartialYAML(t *testing.T) {
	// Only set some new fields; missing ones should get defaults
	content := `
data_dir: /custom/data
rotation:
  period: 6h
cache:
  max_entries: 2000
admin:
  addr: 10.0.0.1:9091
`

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := platform.Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}

	// Set explicitly
	if cfg.DataDir != "/custom/data" {
		t.Errorf("data_dir = %q", cfg.DataDir)
	}
	if cfg.Rotation.Period != 6*time.Hour {
		t.Errorf("rotation.period = %v", cfg.Rotation.Period)
	}
	if cfg.Cache.MaxEntries != 2000 {
		t.Errorf("cache.max_entries = %d", cfg.Cache.MaxEntries)
	}

	// Defaults for stuff not set
	if cfg.BackupDir != "./backups" {
		t.Errorf("backup_dir default = %q", cfg.BackupDir)
	}
	if cfg.Backup.Keep != 3 {
		t.Errorf("backup.keep default = %d", cfg.Backup.Keep)
	}
	if cfg.Cache.TombstoneTTL != time.Hour {
		t.Errorf("cache.tombstone_ttl default = %v", cfg.Cache.TombstoneTTL)
	}
	if cfg.Admin.Validation.UserIDMaxLen != 256 {
		t.Errorf("admin.validation.user_id_max_len default = %d", cfg.Admin.Validation.UserIDMaxLen)
	}
	if cfg.Server.MaxResponseBytes != 10485760 {
		t.Errorf("server.max_response_bytes default = %d", cfg.Server.MaxResponseBytes)
	}
	if cfg.Server.MaxRequestBytes != 10485760 {
		t.Errorf("server.max_request_bytes default = %d", cfg.Server.MaxRequestBytes)
	}
	if cfg.SSRF.DialCheck != false {
		t.Errorf("ssrf.dial_check default = %v, want false", cfg.SSRF.DialCheck)
	}
	if cfg.SSRF.CacheTTL != 30*time.Second {
		t.Errorf("ssrf.cache_ttl default = %v, want 30s", cfg.SSRF.CacheTTL)
	}
	if cfg.SSRF.Timeout != 5*time.Second {
		t.Errorf("ssrf.timeout default = %v, want 5s", cfg.SSRF.Timeout)
	}
	if cfg.Backup.Keep != 3 {
		t.Errorf("backup.keep default = %d", cfg.Backup.Keep)
	}
	if cfg.Backup.KeySnapshot.Keep != 5 {
		t.Errorf("backup.key_snapshot.keep default = %d", cfg.Backup.KeySnapshot.Keep)
	}
	if cfg.Recovery.MaxWait != 5*time.Minute {
		t.Errorf("recovery.max_wait default = %v", cfg.Recovery.MaxWait)
	}
}

func TestValidateInvalidAdminAddr(t *testing.T) {
	content := `
data_dir: ./data
admin:
  addr: "no-port-here"
`

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	_, err := platform.Load(path)
	if err == nil {
		t.Fatal("expected error for invalid admin.addr (no port), got nil")
	}
}

func TestValidateRejectsNegativeNumerics(t *testing.T) {
	// Fields guarded by nonNegative* (allow 0): negative must error.
	// Each row covers a distinct type path (int, int64, duration).
	cases := []struct {
		name    string
		mutate  func(*platform.Config)
		wantSub string
	}{
		{"rotation.period", func(c *platform.Config) { c.Rotation.Period = -1 * time.Hour }, "rotation.period"},
		{"rotation.drain_timeout", func(c *platform.Config) { c.Rotation.DrainTimeout = -1 * time.Second }, "rotation.drain_timeout"},
		{"rotation.complete_drain_timeout", func(c *platform.Config) { c.Rotation.CompleteDrainTimeout = -1 * time.Second }, "rotation.complete_drain_timeout"},
		{"cache.tombstone_ttl", func(c *platform.Config) { c.Cache.TombstoneTTL = -1 * time.Second }, "cache.tombstone_ttl"},
		{"cache.entry_ttl", func(c *platform.Config) { c.Cache.EntryTTL = -1 * time.Second }, "cache.entry_ttl"},
		{"server.shutdown_zero_budget", func(c *platform.Config) { c.Server.ShutdownZeroBudget = -1 * time.Second }, "server.shutdown_zero_budget"},
		{"server.idle_conn_timeout", func(c *platform.Config) { c.Server.IdleConnTimeout = -1 * time.Second }, "server.idle_conn_timeout"},
		{"upstream_timeout_ms", func(c *platform.Config) { c.UpstreamTimeoutMs = -100 }, "upstream_timeout_ms"},
		{"ssrf.cache_ttl", func(c *platform.Config) { c.SSRF.CacheTTL = -1 * time.Second }, "ssrf.cache_ttl"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := platform.Default()
			tc.mutate(&cfg)
			err := cfg.Validate()
			if err == nil {
				t.Fatalf("expected error for %s, got nil", tc.name)
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Errorf("error = %q, want substring %q", err.Error(), tc.wantSub)
			}
		})
	}
}

func TestValidateRejectsNegativeAndZeroForMustBePositive(t *testing.T) {
	// Fields guarded by mustBePositive* reject 0 too — zero is meaningless
	// for these (loop counters, byte caps, keep counts, mandatory
	// timeouts).
	type fieldCase struct {
		name        string
		setNegative func(*platform.Config)
		setZero     func(*platform.Config)
	}
	cases := []fieldCase{
		{"rotation.max_phase_a_loops",
			func(c *platform.Config) { c.Rotation.MaxPhaseALoops = -5 },
			func(c *platform.Config) { c.Rotation.MaxPhaseALoops = 0 }},
		{"cache.max_entries",
			func(c *platform.Config) { c.Cache.MaxEntries = -1 },
			func(c *platform.Config) { c.Cache.MaxEntries = 0 }},
		{"server.max_response_bytes",
			func(c *platform.Config) { c.Server.MaxResponseBytes = -1 },
			func(c *platform.Config) { c.Server.MaxResponseBytes = 0 }},
		{"server.max_request_bytes",
			func(c *platform.Config) { c.Server.MaxRequestBytes = -1 },
			func(c *platform.Config) { c.Server.MaxRequestBytes = 0 }},
		{"server.read_header_timeout",
			func(c *platform.Config) { c.Server.ReadHeaderTimeout = -1 * time.Second },
			func(c *platform.Config) { c.Server.ReadHeaderTimeout = 0 }},
		{"server.shutdown_timeout",
			func(c *platform.Config) { c.Server.ShutdownTimeout = -1 * time.Second },
			func(c *platform.Config) { c.Server.ShutdownTimeout = 0 }},
		{"ssrf.timeout",
			func(c *platform.Config) { c.SSRF.Timeout = -1 * time.Second },
			func(c *platform.Config) { c.SSRF.Timeout = 0 }},
		{"admin.validation.user_id_max_len",
			func(c *platform.Config) { c.Admin.Validation.UserIDMaxLen = -1 },
			func(c *platform.Config) { c.Admin.Validation.UserIDMaxLen = 0 }},
		{"admin.validation.real_url_max_len",
			func(c *platform.Config) { c.Admin.Validation.RealURLMaxLen = -1 },
			func(c *platform.Config) { c.Admin.Validation.RealURLMaxLen = 0 }},
		{"admin.validation.key_tag_max_len",
			func(c *platform.Config) { c.Admin.Validation.KeyTagMaxLen = -1 },
			func(c *platform.Config) { c.Admin.Validation.KeyTagMaxLen = 0 }},
		{"admin.validation.api_key_max_len",
			func(c *platform.Config) { c.Admin.Validation.APIKeyMaxLen = -1 },
			func(c *platform.Config) { c.Admin.Validation.APIKeyMaxLen = 0 }},
		{"admin.validation.auth_type_max_len",
			func(c *platform.Config) { c.Admin.Validation.AuthTypeMaxLen = -1 },
			func(c *platform.Config) { c.Admin.Validation.AuthTypeMaxLen = 0 }},
		{"backup.keep",
			func(c *platform.Config) { c.Backup.Keep = -1 },
			func(c *platform.Config) { c.Backup.Keep = 0 }},
		{"backup.key_snapshot.keep",
			func(c *platform.Config) { c.Backup.KeySnapshot.Keep = -1 },
			func(c *platform.Config) { c.Backup.KeySnapshot.Keep = 0 }},
		{"recovery.max_wait",
			func(c *platform.Config) { c.Recovery.MaxWait = -1 * time.Minute },
			func(c *platform.Config) { c.Recovery.MaxWait = 0 }},
	}
	for _, tc := range cases {
		t.Run(tc.name+"_negative", func(t *testing.T) {
			cfg := platform.Default()
			tc.setNegative(&cfg)
			err := cfg.Validate()
			if err == nil {
				t.Fatalf("expected error for negative %s, got nil", tc.name)
			}
			if !strings.Contains(err.Error(), tc.name) {
				t.Errorf("error = %q, want substring %q", err.Error(), tc.name)
			}
		})
		t.Run(tc.name+"_zero", func(t *testing.T) {
			cfg := platform.Default()
			tc.setZero(&cfg)
			err := cfg.Validate()
			if err == nil {
				t.Fatalf("expected error for zero %s, got nil", tc.name)
			}
			if !strings.Contains(err.Error(), tc.name) {
				t.Errorf("error = %q, want substring %q", err.Error(), tc.name)
			}
		})
	}
}

func TestValidateAcceptsZeroForAllowZeroFields(t *testing.T) {
	// Fields guarded by nonNegative* — 0 has documented "off" semantic,
	// must pass Validate(). (Fields with mustBePositive* are tested
	// separately in TestValidateRejectsNegativeAndZeroForMustBePositive.)
	cfg := platform.Default()
	cfg.Rotation.Period = 0
	cfg.Rotation.DrainTimeout = 0
	cfg.Rotation.CompleteDrainTimeout = 0
	cfg.Cache.TombstoneTTL = 0
	cfg.Cache.EntryTTL = 0
	cfg.Server.ShutdownZeroBudget = 0
	cfg.Server.IdleConnTimeout = 0
	cfg.UpstreamTimeoutMs = 0
	cfg.SSRF.CacheTTL = 0
	if err := cfg.Validate(); err != nil {
		t.Fatalf("zero values for allow-zero fields should pass Validate, got %v", err)
	}
}

func TestValidateInvalidBindAddress(t *testing.T) {
	content := `
server:
  bind_address: "not-a-valid-address"
data_dir: ./data
admin:
  addr: 127.0.0.1:8081
`

	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	_, err := platform.Load(path)
	if err == nil {
		t.Fatal("expected error for invalid bind_address, got nil")
	}
}

func TestValidateWildcardBindRejectsEmptyExternalAddress(t *testing.T) {
	cases := []struct{ name, bind string }{
		{"ipv4", "0.0.0.0:8080"},
		{"ipv6", "[::]:8080"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			content := "server:\n  bind_address: \"" + tc.bind + "\"\ndata_dir: ./data\nadmin:\n  addr: 127.0.0.1:8081\n"
			dir := t.TempDir()
			path := filepath.Join(dir, "config.yaml")
			if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
				t.Fatalf("write config: %v", err)
			}
			_, err := platform.Load(path)
			if err == nil {
				t.Fatal("expected wildcard+empty external_address to fail Validate, got nil")
			}
			if !strings.Contains(err.Error(), "external_address") {
				t.Errorf("error %q should mention external_address", err.Error())
			}
		})
	}
}

func TestValidateWildcardBindAcceptsExternalAddress(t *testing.T) {
	content := `
server:
  bind_address: 0.0.0.0:8080
  external_address: http://router.example.com:8080
data_dir: ./data
admin:
  addr: 127.0.0.1:8081
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	if _, err := platform.Load(path); err != nil {
		t.Fatalf("wildcard+external_address should pass Validate, got %v", err)
	}
}

func TestValidateNonWildcardBindAcceptsEmptyExternalAddress(t *testing.T) {
	cfg := platform.Default()
	cfg.Server.BindAddress = "127.0.0.1:8080"
	cfg.Server.ExternalAddress = ""
	if err := cfg.Validate(); err != nil {
		t.Fatalf("non-wildcard bind + empty external_address should pass Validate, got %v", err)
	}
}

func TestValidateWildcardBindRejectsNonHTTPExternalAddress(t *testing.T) {
	content := `
server:
  bind_address: 0.0.0.0:8080
  external_address: "not-a-url"
data_dir: ./data
admin:
  addr: 127.0.0.1:8081
`
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	_, err := platform.Load(path)
	if err == nil {
		t.Fatal("expected non-http external_address to fail Validate, got nil")
	}
}

func TestLoadMinimalYAML(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	content := `
server:
  bind_address: 0.0.0.0:9090
  external_address: http://router.example.com:9090
upstream_timeout_ms: 5000
`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := platform.Load(path)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}
	if cfg.Server.BindAddress != "0.0.0.0:9090" {
		t.Errorf("server.bind_address = %q, want %q", cfg.Server.BindAddress, "0.0.0.0:9090")
	}
}

func TestUpstreamTimeout(t *testing.T) {
	cfg := platform.Default()
	if cfg.UpstreamTimeout() != 30*time.Second {
		t.Errorf("UpstreamTimeout() = %v", cfg.UpstreamTimeout())
	}
}

// TestValidateRejectsAdminPublicBind locks in the security policy:
// admin.addr cannot bind to a wildcard or public IP because admin has
// no authentication and we do not provide TLS. For internet access,
// the operator must terminate TLS at a reverse proxy in front of
// admin (the proxy can bind publicly; admin stays on a private addr).
func TestValidateRejectsAdminPublicBind(t *testing.T) {
	cases := []struct {
		name    string
		addr    string
		wantSub string
	}{
		{"v4 wildcard", "0.0.0.0:8081", "wildcard"},
		{"v6 wildcard", "[::]:8081", "wildcard"},
		{"v6 wildcard long-form", "[0:0:0:0:0:0:0:0]:8081", "wildcard"},
		{"v4 broadcast", "255.255.255.255:8081", "not loopback or private"},
		{"v4-mapped v6 public", "[::ffff:8.8.8.8]:8081", "not loopback or private"},
		{"v4 public IP", "8.8.8.8:8081", "not loopback or private"},
		{"v4 just-below private (172.15)", "172.15.0.1:8081", "not loopback or private"},
		{"v4 just-above private (172.32)", "172.32.0.1:8081", "not loopback or private"},
		{"v6 public documentation prefix", "[2001:db8::1]:8081", "not loopback or private"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := platform.Default()
			cfg.Admin.Addr = tc.addr
			err := cfg.Validate()
			if err == nil {
				t.Fatalf("admin.addr=%q accepted, want error", tc.addr)
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Errorf("error=%q, want substring %q", err.Error(), tc.wantSub)
			}
		})
	}
}

// TestValidateAllowsAdminLoopbackOrPrivate: the policy admits
// loopback (incl. systemd-resolved's 127.0.0.53), private IPv4
// (10/8, 172.16/12, 192.168/16 — including the 172.31.255.255
// top of the 172.16/12 range and the docker bridge 172.17.0.1),
// IPv6 loopback, IPv6 ULA, and link-local.
func TestValidateAllowsAdminLoopbackOrPrivate(t *testing.T) {
	cases := []struct {
		name string
		addr string
	}{
		{"v4 loopback", "127.0.0.1:8081"},
		{"v4 systemd-resolved stub", "127.0.0.53:8081"},
		{"v6 loopback", "[::1]:8081"},
		{"v4 private 10/8", "10.0.0.1:8081"},
		{"v4 private 172.16/12 low end", "172.16.0.1:8081"},
		{"v4 docker bridge 172.17.0.1", "172.17.0.1:8081"},
		{"v4 private 172.16/12 top end", "172.31.255.255:8081"},
		{"v4 private 192.168/16", "192.168.1.1:8081"},
		{"v6 ULA", "[fc00::1]:8081"},
		{"v4 link-local", "169.254.1.1:8081"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := platform.Default()
			cfg.Admin.Addr = tc.addr
			if err := cfg.Validate(); err != nil {
				t.Errorf("admin.addr=%q rejected: %v", tc.addr, err)
			}
		})
	}
}