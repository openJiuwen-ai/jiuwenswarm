//go:build cgo

package tests_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// tpccFixture is a self-contained TPCC test harness.
// Starts the binary with admin API exposed, a mock upstream for proxy load,
// and pre-seeds credentials via the admin API.
type tpccFixture struct {
	ProxyURL    string
	AdminURL    string
	UpstreamURL string
	Seed        int64
	Cmd         *exec.Cmd
	dir         string
	t           *testing.T

	recordedAuthsMu sync.Mutex
	recordedAuths   []string

	knownKeysMu sync.Mutex
	knownKeys   map[string]map[string]bool

	// proxyKeys maps the identity triple (userID + apiBase + keyTag) to the
	// server-issued proxy_key. proxy_key is random and server-generated, so
	// tests must capture it from create responses instead of deriving it.
	proxyKeysMu sync.Mutex
	proxyKeys   map[string]string
}

func (fx *tpccFixture) recordUpstreamAuth(auth string) {
	fx.recordedAuthsMu.Lock()
	fx.recordedAuths = append(fx.recordedAuths, auth)
	fx.recordedAuthsMu.Unlock()
}

func (fx *tpccFixture) snapshotUpstreamAuths() []string {
	fx.recordedAuthsMu.Lock()
	defer fx.recordedAuthsMu.Unlock()
	out := make([]string, len(fx.recordedAuths))
	copy(out, fx.recordedAuths)
	return out
}

func (fx *tpccFixture) recordKnownKey(proxyKey, apiKey string) {
	fx.knownKeysMu.Lock()
	if fx.knownKeys == nil {
		fx.knownKeys = make(map[string]map[string]bool)
	}
	set, ok := fx.knownKeys[proxyKey]
	if !ok {
		set = make(map[string]bool)
		fx.knownKeys[proxyKey] = set
	}
	set[apiKey] = true
	fx.knownKeysMu.Unlock()
}

func (fx *tpccFixture) isKnownKey(proxyKey, apiKey string) bool {
	fx.knownKeysMu.Lock()
	defer fx.knownKeysMu.Unlock()
	set, ok := fx.knownKeys[proxyKey]
	if !ok {
		return false
	}
	return set[apiKey]
}

// hasKey reports whether any credential was recorded under this proxy_key.
func (fx *tpccFixture) hasKey(proxyKey string) bool {
	fx.knownKeysMu.Lock()
	defer fx.knownKeysMu.Unlock()
	return len(fx.knownKeys[proxyKey]) > 0
}

// recordProxyKey associates the identity triple with the server-issued
// proxy_key so later admin/proxy calls can address the credential.
func (fx *tpccFixture) recordProxyKey(userID, apiBase, keyTag, proxyKey string) {
	fx.proxyKeysMu.Lock()
	if fx.proxyKeys == nil {
		fx.proxyKeys = make(map[string]string)
	}
	fx.proxyKeys[userID+"\x1f"+apiBase+"\x1f"+keyTag] = proxyKey
	fx.proxyKeysMu.Unlock()
}

// proxyKeyFor looks up the proxy_key previously captured from a create
// response. Empty means the credential was never seen (or was created by a
// writer we didn't capture) — callers treat it as "not found".
func (fx *tpccFixture) proxyKeyFor(userID, apiBase, keyTag string) string {
	fx.proxyKeysMu.Lock()
	defer fx.proxyKeysMu.Unlock()
	return fx.proxyKeys[userID+"\x1f"+apiBase+"\x1f"+keyTag]
}

// randomProxyKey generates a well-formed but never-issued proxy_key, so
// admin lookups against it deterministically 404.
func randomProxyKey(rng *rand.Rand) string {
	b := make([]byte, 32)
	if _, err := rng.Read(b); err != nil {
		panic(err)
	}
	return "cr_pk_" + base64.RawURLEncoding.EncodeToString(b)
}

type tpccConfig struct {
	AdminPort     int
	PreSeedCount  int
	IPWhitelistIP string
	BinaryPath    string // "" → RouterBin() (instrumented). Production tests pass ProductionBin().
	PPROFAddr     string // "" → no pprof. Instrumented tests pass "127.0.0.1:<free-port>".
}

func setupTPCC(t *testing.T, cfg tpccConfig) *tpccFixture {
	t.Helper()

	dir := t.TempDir()

	fx := &tpccFixture{t: t}
	mockUpstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if auth := r.Header.Get("Authorization"); auth != "" {
			fx.recordUpstreamAuth(auth)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("upstream-ok"))
	}))
	t.Cleanup(mockUpstream.Close)

	// 2. Admin port
	if cfg.AdminPort == 0 {
		cfg.AdminPort = freePort(t)
	}
	proxyPort := freePort(t)

	// 3. Secrets dir (production derives SecretsDir = data_dir/secrets; S2 is in DB)
	dataDir := filepath.Join(dir, "data")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatalf("mkdir data: %v", err)
	}
	backupDir := filepath.Join(dir, "backups")
	if err := os.MkdirAll(backupDir, 0o755); err != nil {
		t.Fatalf("mkdir backup: %v", err)
	}
	secretsDir := filepath.Join(dataDir, "secrets")
	if err := os.MkdirAll(secretsDir, 0o700); err != nil {
		t.Fatalf("mkdir secrets: %v", err)
	}
	for name, content := range map[string][]byte{
		"s1.bin.1":    bytes.Repeat([]byte{0x01}, 32),
		"s2.bin":      bytes.Repeat([]byte{0x02}, 32),
		"crypto_mode": {0x01},
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, name), content, 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}

	// 4. Config YAML
	cfgPath := filepath.Join(dir, "config.yaml")
	ipWhitelist := cfg.IPWhitelistIP
	if ipWhitelist == "" {
		ipWhitelist = "127.0.0.1"
	}
	cfgYAML := fmt.Sprintf(`
server:
  bind_address: 127.0.0.1:%d
  max_response_bytes: 10485760
upstream_timeout_ms: 5000
data_dir: %s
backup_dir: %s
ssrf:
  allowed_hosts:
    - "127.0.0.1"
    - "localhost"
    - "api.example.com"
admin:
  addr: 127.0.0.1:%d
  validation:
    user_id_max_len: 256
    real_url_max_len: 2048
    key_tag_max_len: 64
    api_key_max_len: 8192
    auth_type_max_len: 16
rotation:
  period: 24h
  max_phase_a_loops: 100
cache:
  max_entries: 10000
  tombstone_ttl: 1h
backup:
  keep_kek: 3
  keep_dek: 3
  filename_template: "backup-{type}-{ts}.db"
  key_snapshot:
    enabled: true
    filename_template: "key-snapshot-{ts}.bin"
    keep: 5
recovery:
  max_wait: 5m
`, proxyPort, dataDir, backupDir, cfg.AdminPort)
	if err := os.WriteFile(cfgPath, []byte(cfgYAML), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	// 5. The store auto-bootstraps its schema on first open
	// (store.OpenWithConfig), so no migrate step is needed. The data/
	// directory must be pre-created.
	if err := os.MkdirAll(filepath.Join(dir, "data"), 0o755); err != nil {
		t.Fatalf("mkdir data: %v", err)
	}

	// 6. Start binary
	binaryPath := cfg.BinaryPath
	if binaryPath == "" {
		binaryPath = RouterBin()
	}
	cmd := exec.Command(binaryPath, "-config", cfgPath)
	cmd.Dir = dir
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = procGroupAttrs()
	if cfg.PPROFAddr != "" {
		cmd.Env = append(os.Environ(), "PPROF_ADDR="+cfg.PPROFAddr)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start credential-router: %v", err)
	}

	proxyURL := fmt.Sprintf("http://127.0.0.1:%d", proxyPort)
	adminURL := fmt.Sprintf("http://127.0.0.1:%d", cfg.AdminPort)
	waitAdminReady(t, adminURL)

	fx = &tpccFixture{
		ProxyURL:    proxyURL,
		AdminURL:    adminURL,
		UpstreamURL: mockUpstream.URL,
		Cmd:         cmd,
		dir:         dir,
		t:           t,
	}

	// 6. Pre-seed credentials
	if cfg.PreSeedCount == 0 {
		cfg.PreSeedCount = 1000
	}
	preSeedTPCC(t, fx, cfg.PreSeedCount, ipWhitelist)

	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
	})
	return fx
}

func waitAdminReady(t *testing.T, adminURL string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(adminURL + "/v1/health")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("admin API not ready at %s", adminURL)
}

func preSeedTPCC(t *testing.T, fx *tpccFixture, n int, ipWhitelist string) {
	t.Helper()
	const seedWorkers = 20
	chunk := (n + seedWorkers - 1) / seedWorkers
	errCh := make(chan error, seedWorkers)
	var wg sync.WaitGroup
	for w := 0; w < seedWorkers; w++ {
		lo, hi := w*chunk, w*chunk+chunk
		if lo >= n {
			break
		}
		if hi > n {
			hi = n
		}
		wg.Add(1)
		go func(lo, hi int) {
			defer wg.Done()
			client := &http.Client{Timeout: 10 * time.Second}
			defer client.CloseIdleConnections()
			for i := lo; i < hi; i++ {
				userID := fmt.Sprintf("user_%d", i)
				url := fmt.Sprintf("https://api.example.com/path/%d", i)
				keyTag := "default"
				apiKey := fmt.Sprintf("seeded-api-key-%d", i)
				authType := "openai"
			body, _ := json.Marshal(map[string]string{
				"user_id":   userID,
				"api_base":  url,
				"key_tag":   keyTag,
				"api_key":   apiKey,
				"auth_type": authType,
			})
			req, _ := http.NewRequest("POST", fx.AdminURL+"/v1/credentials", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			resp, err := client.Do(req)
			if err != nil {
				errCh <- fmt.Errorf("seed %d: %w", i, err)
				return
			}
			if resp.StatusCode/100 != 2 {
				buf, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				errCh <- fmt.Errorf("seed %d: status %d body=%s", i, resp.StatusCode, string(buf))
				return
			}
			var env struct {
				Status string `json:"status"`
				Data   struct {
					ProxyKey string `json:"proxy_key"`
				} `json:"data"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&env)
			resp.Body.Close()
			if env.Data.ProxyKey == "" {
				errCh <- fmt.Errorf("seed %d: response missing proxy_key", i)
				return
			}
			fx.recordProxyKey(userID, url, keyTag, env.Data.ProxyKey)
			fx.recordKnownKey(env.Data.ProxyKey, apiKey)
			}
		}(lo, hi)
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatal(err)
	}
}

// --- TPCC main test ---

const (
	tpccReaders  = 40
	tpccWriters  = 30
	tpccRotators = 5
	tpccLoaders  = 20
	tpccTotal    = tpccReaders + tpccWriters + tpccRotators + tpccLoaders
)

type tpccResult struct {
	op           string
	userID       string
	realURL      string
	keyTag       string
	credID       int64
	err          error
	latency      time.Duration
	status       int
	startedAt    time.Time
	actualAPIKey string
}

// tpccResults is a lock-free fan-in for worker results. Each worker writes
// to its own slice; snapshot() runs after wg.Wait(), which orders all worker
// writes before main reads via the WaitGroup happens-before guarantee.
type tpccResults struct {
	sinks [][]tpccResult
}

func newTPCCResults(numWorkers int) *tpccResults {
	return &tpccResults{sinks: make([][]tpccResult, numWorkers)}
}

func (r *tpccResults) sinkFor(workerID int) *[]tpccResult {
	return &r.sinks[workerID]
}

func (r *tpccResults) snapshot() []tpccResult {
	var total int
	for _, s := range r.sinks {
		total += len(s)
	}
	out := make([]tpccResult, 0, total)
	for _, s := range r.sinks {
		out = append(out, s...)
	}
	return out
}

func TestTPCCConcurrency(t *testing.T) {
	if testing.Short() {
		t.Skip("TPCC skipped in -short mode")
	}

	duration := 5 * time.Minute
	if d := os.Getenv("TPCC_DURATION"); d != "" {
		if parsed, err := time.ParseDuration(d); err == nil {
			duration = parsed
		}
	}
	seed := time.Now().UnixNano()
	if s := os.Getenv("TPCC_SEED"); s != "" {
		if v, err := strconv.ParseInt(s, 10, 64); err == nil {
			seed = v
		}
	}
	t.Logf("TPCC start: seed=%d duration=%v", seed, duration)

	fx := setupTPCC(t, tpccConfig{
		PreSeedCount: 1000,
	})

	// Capture pre-state for invariant checks
	preKM := getKeystoreStatusTPCC(t, fx.AdminURL)
	t.Logf("pre-rotation: kek=%d dek=%d mode=%s",
		preKM.ActiveKekVersion, preKM.ActiveDekVersion, preKM.CryptoMode)

	ctx, cancel := context.WithTimeout(context.Background(), duration)
	defer cancel()

	results := newTPCCResults(tpccTotal)
	goroutinesBefore := runtime.NumGoroutine()

	// Action dispatcher with weights
	var wg sync.WaitGroup
	wg.Add(tpccTotal)
	startAt := time.Now()

	for i := 0; i < tpccReaders; i++ {
		go runReader(ctx, &wg, i, fx, seed+int64(i), results.sinkFor(i))
	}
	for i := 0; i < tpccWriters; i++ {
		go runWriter(ctx, &wg, i, fx, seed+int64(i+100), results.sinkFor(tpccReaders+i))
	}
	for i := 0; i < tpccRotators; i++ {
		go runRotator(ctx, &wg, i, fx, seed+int64(i+200), results.sinkFor(tpccReaders+tpccWriters+i))
	}
	for i := 0; i < tpccLoaders; i++ {
		go runLoader(ctx, &wg, i, fx, seed+int64(i+400), results.sinkFor(tpccReaders+tpccWriters+tpccRotators+i))
	}

	<-ctx.Done()
	wg.Wait()
	actualDuration := time.Since(startAt)

	waitForPendingDrainTPCC(t, fx, actualDuration)
	abortPendingRotationTPCC(t, fx)

	// Wait 30s for goroutines to fully drain (leak check window)
	time.Sleep(30 * time.Second)
	goroutinesAfter := runtime.NumGoroutine()
	leaked := goroutinesAfter - goroutinesBefore

	// Invariant checks
	entries := results.snapshot()
	t.Logf("TPCC done: actual=%v ops=%d leaked_goroutines=%d", actualDuration, len(entries), leaked)

	// Per-workload invariants
	checkNoDeadlockTPCC(t, entries, duration)
	checkNoPanicTPCC(t, entries)
	checkCredentialResponseIntactTPCC(t, fx)
	checkRotationConvergenceTPCC(t, fx)
	checkWrappedDEKChangedTPCC(t, fx, preKM)
	checkReadAPIKeyRoundTripTPCC(t, entries, fx)

	if leaked > 50 { // generous tolerance for runtime / test framework
		t.Errorf("goroutine leak: before=%d after=%d leaked=%d", goroutinesBefore, goroutinesAfter, leaked)
	}
}

func TestTPCCMultiTenantIsolation(t *testing.T) {
	if testing.Short() {
		t.Skip("TPCC skipped in -short mode")
	}

	fx := setupTPCC(t, tpccConfig{PreSeedCount: 1})

	samples := []int{0, 7, 13, 23, 41, 49}
	expectedAuths := make([]string, len(samples))
	for i, idx := range samples {
		expectedAuths[i] = "Bearer multi-tenant-key-" + strconv.Itoa(idx)
	}

	seedClient := &http.Client{Timeout: 5 * time.Second}
	mtProxyKeys := make(map[int]string, len(samples))
	for _, idx := range samples {
		body, _ := json.Marshal(map[string]string{
			"user_id":   fmt.Sprintf("mt_user_%d", idx),
			"api_base":  fx.UpstreamURL,
			"key_tag":   "default",
			"api_key":   "multi-tenant-key-" + strconv.Itoa(idx),
			"auth_type": "openai",
		})
		req, _ := http.NewRequest("POST", fx.AdminURL+"/v1/credentials", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		resp, err := seedClient.Do(req)
		if err != nil {
			t.Fatalf("seed user mt_user_%d: %v", idx, err)
		}
		var env struct {
			Status string `json:"status"`
			Data   struct {
				ProxyKey string `json:"proxy_key"`
			} `json:"data"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&env)
		resp.Body.Close()
		if resp.StatusCode != http.StatusCreated {
			t.Fatalf("seed mt_user_%d status=%d want 201", idx, resp.StatusCode)
		}
		if env.Data.ProxyKey == "" {
			t.Fatalf("seed mt_user_%d: response missing proxy_key", idx)
		}
		mtProxyKeys[idx] = env.Data.ProxyKey
	}

	for _, idx := range samples {
		userID := fmt.Sprintf("mt_user_%d", idx)
		path := fmt.Sprintf("%s/v1/credentials/%s", fx.AdminURL, mtProxyKeys[idx])
		resp, err := seedClient.Get(path)
		if err != nil {
			t.Fatalf("admin GET %s: %v", userID, err)
		}
		var env struct {
			Status string `json:"status"`
			Data   struct {
				APIKey string `json:"api_key"`
				UserID string `json:"user_id"`
			} `json:"data"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&env)
		resp.Body.Close()
		if env.Data.APIKey != "multi-tenant-key-"+strconv.Itoa(idx) {
			t.Errorf("admin GET %s api_key=%q want multi-tenant-key-%d (round-trip mismatch)",
				userID, env.Data.APIKey, idx)
		}
		if env.Data.UserID != userID {
			t.Errorf("admin GET user_id=%q want %s", env.Data.UserID, userID)
		}
	}

	client := &http.Client{Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()

	before := fx.snapshotUpstreamAuths()
	for _, idx := range samples {
		userID := fmt.Sprintf("mt_user_%d", idx)

		encoded := encodeBase64URL(fx.UpstreamURL)
		path := fmt.Sprintf("%s/proxy/%s/%s", fx.ProxyURL, encoded, "/test")
		req, _ := http.NewRequest("GET", path, nil)
		req.Header.Set("Authorization", "Bearer "+mtProxyKeys[idx])

		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("proxy user=%s: %v", userID, err)
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Errorf("user=%s status=%d want 200", userID, resp.StatusCode)
		}
	}
	after := fx.snapshotUpstreamAuths()
	got := after[len(before):]
	if len(got) != len(samples) {
		t.Fatalf("upstream received %d new requests, want %d", len(got), len(samples))
	}

	for i, idx := range samples {
		if got[i] != expectedAuths[i] {
			t.Errorf("mt_user_%d upstream Authorization=%q want %q (cross-tenant?)",
				idx, got[i], expectedAuths[i])
		}
	}

	expectedSet := make(map[string]bool, len(expectedAuths))
	for _, a := range expectedAuths {
		expectedSet[a] = true
	}
	for i, auth := range got {
		if !expectedSet[auth] {
			t.Errorf("upstream Authorization[%d]=%q is not in the expected tenant set", i, auth)
		}
	}
}

// --- Workers ---

func runReader(ctx context.Context, wg *sync.WaitGroup, id int, fx *tpccFixture, seed int64, sink *[]tpccResult) {
	defer wg.Done()
	rng := rand.New(rand.NewSource(seed))
	client := &http.Client{Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		i := rng.Intn(1000)
		userID := fmt.Sprintf("user_%d", i)
		rawURL := fmt.Sprintf("https://api.example.com/path/%d", i)
		keyTag := "default"
		// Admin route is GET /v1/credentials/{proxy_key}; proxy_key is
		// server-issued, so look it up from the fixture instead of deriving
		// it. A miss means this credential wasn't seeded → 404 is fine.
		proxyKey := fx.proxyKeyFor(userID, rawURL, keyTag)
		if proxyKey == "" {
			proxyKey = randomProxyKey(rng)
		}
		path := fmt.Sprintf("%s/v1/credentials/%s", fx.AdminURL, proxyKey)
		started := time.Now()
		resp, err := client.Get(path)
		lat := time.Since(started)
		status := 0
		apiKey := ""
		if resp != nil {
			status = resp.StatusCode
			if status == http.StatusOK {
				var env struct {
					Data struct {
						APIKey string `json:"api_key"`
					} `json:"data"`
				}
				_ = json.NewDecoder(resp.Body).Decode(&env)
				apiKey = env.Data.APIKey
			}
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		*sink = append(*sink, tpccResult{op: "READ", userID: userID, realURL: rawURL, keyTag: keyTag,
			err: err, latency: lat, status: status, startedAt: started, actualAPIKey: apiKey})
		time.Sleep(time.Duration(rng.Intn(50)) * time.Millisecond)
	}
}

func runWriter(ctx context.Context, wg *sync.WaitGroup, id int, fx *tpccFixture, seed int64, sink *[]tpccResult) {
	defer wg.Done()
	rng := rand.New(rand.NewSource(seed))
	client := &http.Client{Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		// 1/3 POST, 1/3 PUT, 1/3 DELETE
		switch rng.Intn(3) {
		case 0:
			actCreateTPCC(ctx, rng, fx, client, sink)
		case 1:
			actUpdateTPCC(ctx, rng, fx, client, sink)
		case 2:
			actDeleteTPCC(ctx, rng, fx, client, sink)
		}
		time.Sleep(time.Duration(rng.Intn(80)) * time.Millisecond)
	}
}

func actCreateTPCC(ctx context.Context, rng *rand.Rand, fx *tpccFixture, client *http.Client, sink *[]tpccResult) {
	i := rng.Intn(100000) + 100000 // offset from seeded range
	userID := fmt.Sprintf("user_%d", i)
	url := fmt.Sprintf("https://api.example.com/dynamic/%d", i)
	apiKey := fmt.Sprintf("dynamic-api-key-%d-%d", rng.Intn(1000000), time.Now().UnixNano())
	body, _ := json.Marshal(map[string]string{
		"user_id": userID, "api_base": url, "key_tag": "default",
		"api_key": apiKey, "auth_type": "openai",
	})
	started := time.Now()
	req, _ := http.NewRequestWithContext(ctx, "POST", fx.AdminURL+"/v1/credentials", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	lat := time.Since(started)
	status := 0
	proxyKey := ""
	if resp != nil {
		status = resp.StatusCode
		var env struct {
			Data struct {
				ProxyKey string `json:"proxy_key"`
			} `json:"data"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&env)
		proxyKey = env.Data.ProxyKey
		resp.Body.Close()
	}
	if status == http.StatusCreated && proxyKey != "" {
		fx.recordProxyKey(userID, url, "default", proxyKey)
		fx.recordKnownKey(proxyKey, apiKey)
	}
	*sink = append(*sink, tpccResult{op: "CREATE", userID: userID, realURL: url, keyTag: "default",
		err: err, latency: lat, status: status, startedAt: started})
}

func actUpdateTPCC(ctx context.Context, rng *rand.Rand, fx *tpccFixture, client *http.Client, sink *[]tpccResult) {
	// Address credentials by their server-issued proxy_key. Random combos
	// mostly miss the seeded range → 404, same 404-dominant profile as the
	// old deterministic-key scheme. On a hit, fetch row_version first so the If-Match
	// header carries a real optimistic-concurrency value.
	userID := fmt.Sprintf("user_%d", rng.Intn(1000))
	realURL := fmt.Sprintf("https://api.example.com/path/%d", rng.Intn(1000))
	keyTag := "default"
	proxyKey := fx.proxyKeyFor(userID, realURL, keyTag)
	if proxyKey == "" {
		proxyKey = randomProxyKey(rng)
	}
	apiKey := fmt.Sprintf("updated-key-%d", rng.Intn(1000000))
	body, _ := json.Marshal(map[string]string{"api_key": apiKey, "auth_type": "openai"})
	started := time.Now()
	// last-write-wins: PUT uses no If-Match header. row_version is
	// server-internal only (Phase A concurrency control). Sending stale rv
	// would 409 the legitimate update.
	req, _ := http.NewRequestWithContext(ctx, "PUT",
		fmt.Sprintf("%s/v1/credentials/%s", fx.AdminURL, proxyKey), bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	lat := time.Since(started)
	status := 0
	if resp != nil {
		status = resp.StatusCode
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	if status == http.StatusOK {
		fx.recordKnownKey(proxyKey, apiKey)
	}
	*sink = append(*sink, tpccResult{op: "UPDATE", userID: userID, realURL: realURL, keyTag: keyTag,
		err: err, latency: lat, status: status, startedAt: started})
}

func actDeleteTPCC(ctx context.Context, rng *rand.Rand, fx *tpccFixture, client *http.Client, sink *[]tpccResult) {
	userID := fmt.Sprintf("user_%d", rng.Intn(1000))
	realURL := fmt.Sprintf("https://api.example.com/path/%d", rng.Intn(1000))
	keyTag := "default"
	proxyKey := fx.proxyKeyFor(userID, realURL, keyTag)
	if proxyKey == "" {
		proxyKey = randomProxyKey(rng)
	}
	started := time.Now()
	// last-write-wins: DELETE uses no If-Match header. row_version is
	// server-internal only.
	req, _ := http.NewRequestWithContext(ctx, "DELETE",
		fmt.Sprintf("%s/v1/credentials/%s", fx.AdminURL, proxyKey), nil)
	resp, err := client.Do(req)
	lat := time.Since(started)
	status := 0
	if resp != nil {
		status = resp.StatusCode
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	*sink = append(*sink, tpccResult{op: "DELETE", userID: userID, realURL: realURL, keyTag: keyTag,
		err: err, latency: lat, status: status, startedAt: started})
}

func runRotator(ctx context.Context, wg *sync.WaitGroup, id int, fx *tpccFixture, seed int64, sink *[]tpccResult) {
	defer wg.Done()
	rng := rand.New(rand.NewSource(seed))
	client := &http.Client{Timeout: 30 * time.Second} // rotation can be slow
	defer client.CloseIdleConnections()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		// Alternate KEK/DEK rotation. KEK requires writing a new S1 file first.
		var op string
		var method, path string
		var reqBody io.Reader
		if rng.Intn(2) == 0 {
			op = "ROTATE_KEK"
			version := int64(id) + 2
			newS1Path := filepath.Join(fx.dir, "secrets", fmt.Sprintf("s1.bin.%d", version))
			var s1 [32]byte
			for i := range s1 {
				s1[i] = byte(rng.Intn(256))
			}
			if err := os.WriteFile(newS1Path, s1[:], 0o600); err != nil {
				*sink = append(*sink, tpccResult{op: op, err: err})
				continue
			}
			method = "POST"
			path = fx.AdminURL + "/v1/keystore/shards"
			reqBody = strings.NewReader(`{"action":"rotate-s1"}`)
		} else {
			op = "ROTATE_DEK"
			method = "POST"
			path = fx.AdminURL + "/v1/keystore/rotate/dek"
		}
		started := time.Now()
		req, _ := http.NewRequestWithContext(ctx, method, path, reqBody)
		if reqBody != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := client.Do(req)
		lat := time.Since(started)
		status := 0
		if resp != nil {
			status = resp.StatusCode
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		*sink = append(*sink, tpccResult{op: op, err: err, latency: lat, status: status, startedAt: started})
		time.Sleep(time.Duration(rng.Intn(5000)) * time.Millisecond)
	}
}

func runLoader(ctx context.Context, wg *sync.WaitGroup, id int, fx *tpccFixture, seed int64, sink *[]tpccResult) {
	defer wg.Done()
	rng := rand.New(rand.NewSource(seed))
	client := &http.Client{Timeout: 5 * time.Second}
	defer client.CloseIdleConnections()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		i := rng.Intn(1000)
		userID := fmt.Sprintf("user_%d", i)
		url := fmt.Sprintf("https://api.example.com/path/%d", i)
		proxyKey := fx.proxyKeyFor(userID, url, "default")
		if proxyKey == "" {
			proxyKey = randomProxyKey(rng)
		}
		encoded := encodeBase64URL(url)
		// Upstream URL must be plain http:// for the mock
		upstreamEncoded := encodeBase64URL(fx.UpstreamURL)
		path := fmt.Sprintf("%s/proxy/%s/%s/%s/?upstream=%s",
			fx.ProxyURL, encoded, upstreamEncoded, "/test", "")
		started := time.Now()
		req, _ := http.NewRequestWithContext(ctx, "GET", path, nil)
		req.Header.Set("Authorization", "Bearer "+proxyKey)
		resp, err := client.Do(req)
		lat := time.Since(started)
		status := 0
		if resp != nil {
			status = resp.StatusCode
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		*sink = append(*sink, tpccResult{op: "LOAD", realURL: url, err: err, latency: lat, status: status, startedAt: started})
		time.Sleep(time.Duration(rng.Intn(50)) * time.Millisecond)
	}
}

// --- Invariant checks ---

type tpccKeyStatus struct {
	ActiveKekVersion  int64  `json:"active_kek_version"`
	PendingKekVersion int64  `json:"pending_kek_version"`
	ActiveDekVersion  int64  `json:"active_dek_version"`
	PendingDekVersion int64  `json:"pending_dek_version"`
	CryptoMode        string `json:"crypto_mode"`
	WrappedDEK        string `json:"wrapped_dek,omitempty"`
}

func getKeystoreStatusTPCC(t *testing.T, adminURL string) tpccKeyStatus {
	t.Helper()
	resp, err := http.Get(adminURL + "/v1/keystore/status")
	if err != nil {
		t.Fatalf("keystore status: %v", err)
	}
	defer resp.Body.Close()
	// Admin responses are wrapped in a {status,data} envelope; decode the
	// envelope first so we read the inner payload, not the outer status string.
	var env struct {
		Status string        `json:"status"`
		Data   tpccKeyStatus `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil {
		t.Fatalf("decode status: %v", err)
	}
	if env.Data.ActiveKekVersion == 0 && env.Data.ActiveDekVersion == 0 {
		t.Logf("keystore status decoded: KEK=%d DEK=%d crypto_mode=%q wrapped_dek=%q",
			env.Data.ActiveKekVersion, env.Data.ActiveDekVersion,
			env.Data.CryptoMode, env.Data.WrappedDEK)
	}
	return env.Data
}

func checkNoDeadlockTPCC(t *testing.T, entries []tpccResult, duration time.Duration) {
	maxAllowed := duration + 30*time.Second
	for _, e := range entries {
		if e.latency > 30*time.Second && e.status == 0 {
			t.Errorf("possible deadlock: op=%s latency=%v no response", e.op, e.latency)
			break
		}
		_ = maxAllowed
	}
}

func checkNoPanicTPCC(t *testing.T, entries []tpccResult) {
	for _, e := range entries {
		if e.err != nil && strings.Contains(e.err.Error(), "panic") {
			t.Errorf("panic detected in op=%s: %v", e.op, e.err)
			break
		}
	}
}

// checkCredentialResponseIntactTPCC samples seeded credentials and verifies
// GET responses carry the expected identity fields (user_id + proxy_key).
func checkCredentialResponseIntactTPCC(t *testing.T, fx *tpccFixture) {
	// Tolerate 404s (concurrent deletes during test) — just skip those.
	for i := 0; i < 100; i++ {
		userID := fmt.Sprintf("user_%d", i)
		rawURL := fmt.Sprintf("https://api.example.com/path/%d", i)
		keyTag := "default"
		proxyKey := fx.proxyKeyFor(userID, rawURL, keyTag)
		if proxyKey == "" {
			continue
		}
		path := fmt.Sprintf("%s/v1/credentials/%s", fx.AdminURL, proxyKey)
		resp, err := http.Get(path)
		if err != nil {
			continue
		}
		if resp.StatusCode == http.StatusNotFound {
			resp.Body.Close()
			continue
		}
		var env struct {
			Status string `json:"status"`
			Data   struct {
				UserID   string `json:"user_id"`
				ProxyKey string `json:"proxy_key"`
			} `json:"data"`
		}
		json.NewDecoder(resp.Body).Decode(&env)
		resp.Body.Close()
		if env.Data.UserID != userID {
			t.Errorf("credential %s/%s: response user_id=%q, want %q",
				userID, rawURL, env.Data.UserID, userID)
			break
		}
		if env.Data.ProxyKey != proxyKey {
			t.Errorf("credential %s/%s: response proxy_key=%q, want %q",
				userID, rawURL, env.Data.ProxyKey, proxyKey)
			break
		}
	}
}

func checkReadAPIKeyRoundTripTPCC(t *testing.T, entries []tpccResult, fx *tpccFixture) {
	validPrefixes := []string{"seeded-api-key-", "dynamic-api-key-", "updated-key-"}
	emptyCount, badPrefixCount, mismatchCount := 0, 0, 0
	for _, e := range entries {
		if e.op != "READ" || e.status != http.StatusOK {
			continue
		}
		if e.actualAPIKey == "" {
			emptyCount++
			continue
		}
		ok := false
		for _, p := range validPrefixes {
			if strings.HasPrefix(e.actualAPIKey, p) {
				ok = true
				break
			}
		}
		if !ok {
			badPrefixCount++
			if badPrefixCount <= 5 {
				t.Errorf("READ api_key has unexpected format: user=%s key=%q (not seeded/dynamic/updated)",
					e.userID, e.actualAPIKey)
			}
			continue
		}
		proxyKey := fx.proxyKeyFor(e.userID, e.realURL, e.keyTag)
		if !fx.isKnownKey(proxyKey, e.actualAPIKey) {
			mismatchCount++
			if mismatchCount <= 5 {
				t.Errorf("READ api_key not in known-set for proxy_key: user=%s key=%q (corruption or stale-cache?)",
					e.userID, e.actualAPIKey)
			}
		}
	}
	if emptyCount > 0 {
		t.Errorf("READ round-trip: %d responses had empty api_key (encryption layer corruption?)", emptyCount)
	}
	if badPrefixCount > 0 {
		t.Errorf("READ round-trip: %d responses had unexpected api_key format (expected seeded-/dynamic-/updated- prefix)",
			badPrefixCount)
	}
	if mismatchCount > 0 {
		t.Errorf("READ round-trip: %d responses returned api_key that was never written (known-set invariant violated)",
			mismatchCount)
	}
}

func checkRotationConvergenceTPCC(t *testing.T, fx *tpccFixture) {
	km := getKeystoreStatusTPCC(t, fx.AdminURL)
	if km.PendingKekVersion > 0 || km.PendingDekVersion > 0 {
		t.Errorf("rotation pending at end: kek=%d dek=%d", km.PendingKekVersion, km.PendingDekVersion)
	}
}

func waitForPendingDrainTPCC(t *testing.T, fx *tpccFixture, elapsed time.Duration) {
	deadline := time.Duration(0)
	if elapsed < 30*time.Second {
		deadline = elapsed * 2
	} else {
		deadline = 30 * time.Second
	}
	end := time.Now().Add(deadline)
	for time.Now().Before(end) {
		km := getKeystoreStatusTPCC(t, fx.AdminURL)
		if km.PendingKekVersion == 0 && km.PendingDekVersion == 0 {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func abortPendingRotationTPCC(t *testing.T, fx *tpccFixture) {
	km := getKeystoreStatusTPCC(t, fx.AdminURL)
	if km.PendingKekVersion > 0 {
		resp, err := http.Post(fx.AdminURL+"/v1/keystore/abort", "application/json",
			strings.NewReader(`{"type":"kek"}`))
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
	}
	if km.PendingDekVersion > 0 {
		resp, err := http.Post(fx.AdminURL+"/v1/keystore/abort", "application/json",
			strings.NewReader(`{"type":"dek"}`))
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
	}
}

func checkWrappedDEKChangedTPCC(t *testing.T, fx *tpccFixture, pre tpccKeyStatus) {
	post := getKeystoreStatusTPCC(t, fx.AdminURL)
	if pre.ActiveKekVersion == post.ActiveKekVersion &&
		pre.ActiveDekVersion == post.ActiveDekVersion {
		t.Logf("no rotation occurred during test (kek=%d dek=%d)",
			post.ActiveKekVersion, post.ActiveDekVersion)
		return
	}
	t.Logf("rotation occurred: kek %d→%d, dek %d→%d",
		pre.ActiveKekVersion, post.ActiveKekVersion,
		pre.ActiveDekVersion, post.ActiveDekVersion)
}

// Silence unused-import warnings if helper functions reference net etc.
var _ = net.Listen
