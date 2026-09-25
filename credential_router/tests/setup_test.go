package tests_test

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

var (
	routerBinPath     string
	productionBinPath string
	buildOnce         sync.Once
	buildErr          error
)

// credentialRouterTestNonexistentProxyKey is a well-formed proxy_key
// ("cr_pk_" + base64url) that no credential matches. Proxy lookups against it
// must resolve to 401 credential-not-found.
const credentialRouterTestNonexistentProxyKey = "cr_pk_" + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

// TestMain builds both the production binary (no tag) and the instrumented
// binary (-tags instrumented) once, then exposes them via RouterBin /
// ProductionBin. Tests that don't care which variant they exercise just call
// RouterBin() and get the instrumented one (existing default); tests that
// specifically want production behavior call ProductionBin().
func TestMain(m *testing.M) {
	buildOnce.Do(func() {
		var err error
		if routerBinPath, err = buildRouterBinary("instrumented", "CREDENTIAL_ROUTER_BIN", "credential-router-test"); err != nil {
			buildErr = err
		} else if productionBinPath, err = buildRouterBinary("production", "CREDENTIAL_ROUTER_PROD_BIN", "credential-router-prod-test"); err != nil {
			buildErr = err
		}
	})
	if buildErr != nil {
		fmt.Fprintf(os.Stderr, "prepare credential-router binary: %v\n", buildErr)
		os.Exit(1)
	}
	code := m.Run()
	killAllTestRouters()
	os.Exit(code)
}

// RouterBin returns the path to the instrumented binary (with -tags
// instrumented). This is the default for tests that don't need to
// distinguish production vs instrumented.
func RouterBin() string { return routerBinPath }

// ProductionBin returns the path to the production binary (no build tag).
func ProductionBin() string { return productionBinPath }

// buildRouterBinary builds the credential-router binary. variant is the
// human-readable name ("instrumented" or "production") used for logging and
// the output filename. tagFlag is the -tags value ("" or "instrumented").
// envOverride is the env var that, if set, bypasses building and uses the
// referenced binary instead (CREDENTIAL_ROUTER_BIN /
// CREDENTIAL_ROUTER_PROD_BIN).
func buildRouterBinary(variant, envOverride, outName string) (string, error) {
	if bin := os.Getenv(envOverride); bin != "" {
		if !filepath.IsAbs(bin) {
			root, err := moduleRoot()
			if err != nil {
				return "", err
			}
			bin = filepath.Join(root, bin)
		}
		if _, err := os.Stat(bin); err != nil {
			return "", fmt.Errorf("%s not found: %w", envOverride, err)
		}
		return bin, nil
	}

	root, err := moduleRoot()
	if err != nil {
		return "", err
	}

	out := filepath.Join(root, "bin", outName)
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
		return "", err
	}

	args := []string{"build", "-o", out}
	if variant == "instrumented" {
		args = append(args, "-tags", "instrumented")
	}
	args = append(args, "./cmd/credential-router")
	cmd := exec.Command("go", args...)
	cmd.Dir = root
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("go build credential-router (%s): %w", variant, err)
	}
	return out, nil
}

func moduleRoot() (string, error) {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		return "", fmt.Errorf("resolve module root: runtime.Caller failed")
	}
	dir := filepath.Dir(file)
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("go.mod not found")
		}
		dir = parent
	}
}

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("allocate port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	return port
}

func encodeBase64URL(raw string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(raw))
}

type routerConfig struct {
	Port          int
	AdminPort     int
	StubUserID    string
	StubSandboxID string
	Credentials   []credentialEntry
	DataDir       string
	BackupDir     string
	AllowedHosts  []string
}

type credentialEntry struct {
	UserID   string `yaml:"user_id,omitempty"`
	APIBase string
	KeyTag   string `yaml:"key_tag"`
	APIKey   string `yaml:"api_key"`
	AuthType string `yaml:"auth_type"`
}

type yamlConfigFile struct {
	Server struct {
		BindAddress string `yaml:"bind_address"`
	} `yaml:"server"`
	DataDir          string `yaml:"data_dir"`
	BackupDir        string `yaml:"backup_dir"`
	UpstreamTimeoutMs int    `yaml:"upstream_timeout_ms"`
	SSRF struct {
		AllowedHosts []string `yaml:"allowed_hosts,omitempty"`
	} `yaml:"ssrf"`
	Admin struct {
		Addr string `yaml:"addr,omitempty"`
	} `yaml:"admin,omitempty"`
}

func (c routerConfig) yamlBytes() ([]byte, error) {
	cfg := yamlConfigFile{
		UpstreamTimeoutMs: 5000,
	}
	cfg.Server.BindAddress = "127.0.0.1:" + fmt.Sprintf("%d", c.Port)
	cfg.DataDir = c.DataDir
	cfg.BackupDir = c.BackupDir
	if c.AdminPort > 0 {
		cfg.Admin.Addr = fmt.Sprintf("127.0.0.1:%d", c.AdminPort)
	}
	cfg.SSRF.AllowedHosts = c.AllowedHosts
	return yaml.Marshal(cfg)
}

type routerProcess struct {
	BaseURL  string
	AdminURL string
}

func startRouter(t *testing.T, cfg routerConfig) *routerProcess {
	t.Helper()
	return startRouterWithBin(t, cfg, RouterBin())
}

// startRouterWithBin is startRouter with an explicit binary path. Tests that
// need production behavior (no pprof instrumentation) pass ProductionBin();
// the default startRouter keeps using RouterBin() (instrumented).
func startRouterWithBin(t *testing.T, cfg routerConfig, binPath string) *routerProcess {
	t.Helper()

	if cfg.Port == 0 {
		cfg.Port = freePort(t)
	}
	if cfg.AdminPort == 0 {
		cfg.AdminPort = freePort(t)
	}
	if cfg.StubUserID == "" {
		cfg.StubUserID = "user_a"
	}
	if cfg.StubSandboxID == "" {
		cfg.StubSandboxID = "vm-001"
	}
	if cfg.AllowedHosts == nil {
		cfg.AllowedHosts = []string{
			"127.0.0.1",
			"localhost",
			"example.com",
			"api.example.com",
			"bulk.example.com",
			"other.com",
			"api.openai.com",
		}
	}

	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")
	data, err := cfg.yamlBytes()
	if err != nil {
		t.Fatalf("marshal config: %v", err)
	}
	if err := os.WriteFile(cfgPath, data, 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	// Mirror production layout: data_dir holds the DB, S1 shard, and the S2
	// secrets subdirectory; backup_dir is sibling. main.go would mkdir these
	// itself, but we create them here so S2 files below land in the right
	// place before the binary opens them.
	cfg.DataDir = filepath.Join(dir, "data")
	cfg.BackupDir = filepath.Join(dir, "backups")
	if err := os.MkdirAll(cfg.DataDir, 0o755); err != nil {
		t.Fatalf("mkdir data: %v", err)
	}
	if err := os.MkdirAll(cfg.BackupDir, 0o755); err != nil {
		t.Fatalf("mkdir backup: %v", err)
	}
	secretsDir := filepath.Join(cfg.DataDir, "secrets")
	if err := os.MkdirAll(secretsDir, 0o700); err != nil {
		t.Fatalf("mkdir secrets: %v", err)
	}
	for name, content := range map[string][]byte{
		"s1.bin.1":    bytes.Repeat([]byte{0x01}, 32),
		"crypto_mode": {0x01},
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, name), content, 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	// Rewrite the config now that DataDir/BackupDir are known.
	data, err = cfg.yamlBytes()
	if err != nil {
		t.Fatalf("marshal config: %v", err)
	}
	if err := os.WriteFile(cfgPath, data, 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cmd := exec.Command(binPath, "-config", cfgPath)
	cmd.Dir = dir
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "PPROF_ADDR="+pprofAddrForBinary(binPath))
	cmd.SysProcAttr = procGroupAttrs()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start credential-router: %v", err)
	}
	baseURL := fmt.Sprintf("http://127.0.0.1:%d", cfg.Port)
	adminURL := fmt.Sprintf("http://127.0.0.1:%d", cfg.AdminPort)
	waitRouterReady(t, baseURL)

	// Seed credentials via admin API (encryption done by router using its active DEK).
	for _, cred := range cfg.Credentials {
		userID := cred.UserID
		if userID == "" {
			userID = cfg.StubUserID
		}
		seedCredential(t, adminURL, userID, cred.APIBase, cred.KeyTag, cred.APIKey, cred.AuthType)
	}

	t.Cleanup(func() {
		stopRouterProcess(cmd)
	})

	return &routerProcess{BaseURL: baseURL, AdminURL: adminURL}
}

func stopRouterProcess(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	if alive(cmd) {
		_ = signalProcGroup(cmd.Process, interruptSignal())
	}
	done := make(chan struct{})
	go func() { _, _ = cmd.Process.Wait(); close(done) }()
	select {
	case <-done:
		return
	case <-time.After(2 * time.Second):
	}
	if alive(cmd) {
		_ = killProcGroup(cmd.Process)
	}
	select {
	case <-done:
		return
	case <-time.After(2 * time.Second):
	}
	if alive(cmd) {
		_ = cmd.Process.Kill()
		<-done
	}
}

func seedCredential(t *testing.T, baseURL, userID, realURL, keyTag, apiKey, authType string) {
	t.Helper()
	body, _ := json.Marshal(map[string]string{
		"user_id":   userID,
		"api_base": realURL,
		"key_tag":   keyTag,
		"api_key":   apiKey,
		"auth_type": authType,
	})
	url := baseURL + "/v1/credentials"
	req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("seed credential: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		buf, _ := io.ReadAll(resp.Body)
		t.Fatalf("seed credential %s: status %d body=%s", url, resp.StatusCode, string(buf))
	}
	io.Copy(io.Discard, resp.Body)
}

func waitRouterReady(t *testing.T, baseURL string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(baseURL + "/health")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("credential-router not ready at %s", baseURL)
}

// proxyURL builds the proxy URL for the api_base+path form: the api_base is
// base64url-encoded into the first path segment and the credential's proxy_key
// is supplied via the Authorization: Bearer header (see proxyKeyFor).
func proxyURL(baseURL, apiBaseURL, path string) string {
	encoded := encodeBase64URL(apiBaseURL)
	if path == "" {
		path = "/"
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return fmt.Sprintf("%s/proxy/%s%s", baseURL, encoded, path)
}

// proxyKeyFor returns the proxy_key of the credential seeded for the given
// user_id (empty = wildcard), api_base and key_tag, looked up through the admin
// list endpoint. The proxy_key is generated server-side at create time, so
// tests must read it back rather than construct it.
func proxyKeyFor(t *testing.T, router *routerProcess, userID, apiBase, keyTag string) string {
	t.Helper()
	url := router.AdminURL + "/v1/credentials?limit=200"
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("list credentials: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		buf, _ := io.ReadAll(resp.Body)
		t.Fatalf("list credentials %s: status %d body=%s", url, resp.StatusCode, string(buf))
	}
	var env struct {
		Data struct {
			Items []struct {
				UserID   string `json:"user_id"`
				APIBase  string `json:"api_base"`
				KeyTag   string `json:"key_tag"`
				ProxyKey string `json:"proxy_key"`
			} `json:"items"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatalf("decode list response: %v", err)
	}
	for _, it := range env.Data.Items {
		if it.APIBase == apiBase && it.KeyTag == keyTag && (userID == "" || it.UserID == userID) {
			return it.ProxyKey
		}
	}
	t.Fatalf("no credential with user_id=%q api_base=%q key_tag=%q", userID, apiBase, keyTag)
	return ""
}

// pprofAddrForBinary returns the PPROF_ADDR to set on the spawned binary.
// The production test binary has no pprof code (gated behind -tags instrumented),
// so it must not be told to listen on a pprof port.
func pprofAddrForBinary(binPath string) string {
	if v := os.Getenv("PPROF_ADDR"); v != "" {
		return v
	}
	if strings.HasSuffix(binPath, "-prod-test") {
		return ""
	}
	if strings.HasSuffix(binPath, "-test") {
		return "127.0.0.1:6060"
	}
	return ""
}

// killAllTestRouters is the safety net for when t.Cleanup does not run —
// outer `go test` killed by Ctrl-C, panic in test binary, harness timeout.
// credential-router-test runs in its own process group (Setpgid:true), so
// killing the group works even after reparenting to init.
func killAllTestRouters() {
	if runtime.GOOS == "windows" {
		return
	}
	out, err := exec.Command("pgrep", "-P", fmt.Sprint(os.Getpid()), "-f", "credential-router-test").Output()
	if err == nil {
		for _, pidStr := range splitNonEmptyLines(string(out)) {
			if pid, atoiErr := atoiSafe(pidStr); atoiErr == nil {
				_ = exec.Command("kill", "--", fmt.Sprint(-pid)).Run()
			}
		}
	}
	time.Sleep(200 * time.Millisecond)
	out2, _ := exec.Command("pgrep", "-f", "credential-router-test").Output()
	for _, pidStr := range splitNonEmptyLines(string(out2)) {
		pid, perr := atoiSafe(pidStr)
		if perr != nil {
			continue
		}
		_ = exec.Command("kill", "--", fmt.Sprint(-pid)).Run()
	}
}

func atoiSafe(s string) (int, error) {
	var n int
	for _, c := range []byte(s) {
		if c < '0' || c > '9' {
			return 0, fmt.Errorf("invalid int %q", s)
		}
		n = n*10 + int(c-'0')
	}
	return n, nil
}

func splitNonEmptyLines(s string) []string {
	var out []string
	start := 0
	for i, c := range []byte(s) {
		if c == '\n' {
			if i > start {
				out = append(out, s[start:i])
			}
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
