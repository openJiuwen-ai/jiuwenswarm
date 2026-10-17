//go:build cgo

// TestE2E_KEKRunbook_CrashMidRotation is a black-box end-to-end test of the
// full operator runbook for keystore crash recovery:
//
//	build binary → programmatic install (SelfInit) → start server → POST a
//	credential → trigger KEK S1 rotation via admin API → SIGKILL mid-rotation
//	→ restart server → verify the binary comes back up cleanly AND can still
//	decrypt the credential.
//
// Each round uses a fresh temp dir (fresh secrets + SQLite DB), a freshly
// built production binary, and its own ephemeral listen ports, so rounds and
// concurrent test runs cannot collide. Runs are sequential (no t.Parallel).
//
// The test is opt-in: `go test -run TestE2E_KEKRunbook ./tests/...`, and is
// skipped entirely under `go test -short`.
package tests_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"

	"gopkg.in/yaml.v3"
)

// seedBulkCreds is the number of extra credential rows written during the
// programmatic install. The KEK S1 rotation for a single credential completes
// in ~10ms — far too fast to reliably SIGKILL mid-flight. With N bulk rows the
// Phase-B bulk UPDATE (BulkUpdateKekVersion) runs for tens of milliseconds,
// giving the test a deterministic, comfortably wide window in which to kill
// the process after the DB has recorded pending_kek_version>0 but before the
// rotation commits.
const seedBulkCreds = 20000

const (
	runbookUser   = "u1"
	runbookURL    = "https://api.example.com/" // POSTed as-is; server normalizes
	runbookKeyTag = "default"
	runbookAPIKey = "sk-test123"
)

// runbookAdminAddr is the admin base address of the currently-running round's
// server. Rounds run sequentially (no t.Parallel), so a single package-level
// slot is safe.
var runbookAdminAddr string

// runbookProxyKey is the proxy_key minted for the runbook credential by the
// currently-running round's POST /v1/credentials. The server mints proxy_key at
// INSERT (it is not derivable client-side), so the round captures it from the
// create response and reuses it for the read-back assertions.
var runbookProxyKey string

// runbookSrvLog holds the captured stdout/stderr of the most recently started
// server, so failure messages can include the server's own logs.
var runbookSrvLog struct {
	stdout *bytes.Buffer
	stderr *bytes.Buffer
}

func TestE2E_KEKRunbook_CrashMidRotation(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping KEK runbook crash-recovery e2e in -short mode")
	}
	// 5 rounds cover different SIGKILL timings: rounds 0-3 kill while the
	// rotation is in flight (recovery Case 4 — KEK S1 forward); round 4 waits
	// for the rotation to complete before killing (recovery Case 1 — clean).
	for round := 0; round < 5; round++ {
		t.Run(fmt.Sprintf("round-%d", round), func(t *testing.T) {
			runOneRound(t, round)
		})
	}
}

// runOneRound executes one full crash-recovery cycle.
func runOneRound(t *testing.T, round int) {
	tmpDir := t.TempDir()
	dataDir := filepath.Join(tmpDir, "data")
	secretsDir := filepath.Join(dataDir, "secrets")
	binDir := filepath.Join(tmpDir, "bin")
	for _, d := range []string{dataDir, secretsDir, binDir} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatalf("mkdir %s: %v", d, err)
		}
	}

	// 1. Build the production binary (no build tags) into this round's bin dir.
	binPath := buildBinary(t, binDir)

	// 2. Programmatic install: pre-write secrets, run SelfInit (this is what
	//    scripts/start.sh install does, minus the shell + systemd), seed bulk
	//    rows, and close the DB before the server opens it.
	programmaticInstall(t, secretsDir, dataDir)

	// 3. Start server #1.
	server1, adminAddr, cleanup1 := startServer(t, binPath, secretsDir, dataDir)
	runbookAdminAddr = adminAddr
	defer cleanup1()
	waitForIdle(t, adminAddr, 10*time.Second)

	// 4. POST a credential and verify baseline decryption.
	postCredential(t, adminAddr)
	verifyCredentialDecrypt(t, adminAddr, 1)

	// 5. Fire the KEK S1 rotation (server auto-generates S1 internally);
	//    do NOT wait for the response.
	rotDone := make(chan string, 1)
	go func() {
		body := `{"action":"rotate-s1"}`
		resp, err := http.Post("http://"+adminAddr+"/v1/keystore/shards", "application/json", strings.NewReader(body))
		if err != nil {
			rotDone <- "client-error: " + err.Error()
			return
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		rotDone <- fmt.Sprintf("status=%d body=%s", resp.StatusCode, strings.TrimSpace(string(b)))
	}()

	// 6. Randomly-timed SIGKILL. Rounds 0-3 kill ~0-3ms after the rotation
	//    becomes observable in the DB (guaranteed mid-flight). Round 4 delays
	//    2s so the rotation fully completes before the kill.
	minMs, maxMs := 0, 3
	if round == 4 {
		minMs, maxMs = 2000, 2000
	}
	randomKillMidRotation(t, server1.Process.Pid, server1, minMs, maxMs)

	// The rotation request result: for rounds 0-3 this is a client error
	// (connection reset mid-rotation — expected); for round 4 it is the
	// completed rotation response. Only an explicit HTTP error status is fatal.
	rot := <-rotDone
	if strings.HasPrefix(rot, "status=") && !strings.HasPrefix(rot, "status=200") {
		t.Fatalf("rotation POST failed: %s", rot)
	}
	t.Logf("rotation request result: %s", rot)

	// 7. Restart on the same binary + secrets/data dirs. Startup exercises
	//    RecoverFromState + RunStartupConvergence.
	_, adminAddr2, cleanup2 := startServer(t, binPath, secretsDir, dataDir)
	defer cleanup2()
	waitForIdle(t, adminAddr2, 10*time.Second)

	// 8. Verify: status shows promoted KEK, no pending rotation; the
	//    credential decrypts (wrapped_dek unwrap works post-recovery).
	verifyStatusPostRecovery(t, adminAddr2)
	verifyCredentialDecrypt(t, adminAddr2, 2)

	t.Logf("round %d: recovery verified (active_kek_version=2, credential decrypts)", round)
}

// buildBinary compiles the production credential-router binary into outDir.
func buildBinary(t *testing.T, outDir string) string {
	t.Helper()
	root, err := moduleRoot()
	if err != nil {
		t.Fatalf("moduleRoot: %v", err)
	}
	out := filepath.Join(outDir, "credential-router")
	cmd := exec.Command("go", "build", "-o", out, "./cmd/credential-router")
	cmd.Dir = root
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("go build credential-router: %v\nstdout: %s\nstderr: %s", err, stdout.String(), stderr.String())
	}
	return out
}

// programmaticInstall pre-writes the secrets layout that SelfInit expects,
// runs SelfInit on a freshly migrated SQLite DB, sets dek_rotated_at (so the
// server's startup auto-DEK-rotate skips — this test is about KEK, not DEK),
// seeds bulk rows, and closes the DB.
func programmaticInstall(t *testing.T, secretsDir, dataDir string) {
	t.Helper()

	// Pre-write secrets in SelfInit's expected layout: s1.bin.1 (32 bytes),
	// dek.bin (16 bytes), crypto_mode (1-byte binary 0x01=aes).
	var s1 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	for name, content := range map[string][]byte{
		"s1.bin.1": s1[:],
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, name), content, 0o600); err != nil {
			t.Fatalf("write secrets/%s: %v", name, err)
		}
	}
	if err := keystore.WriteCryptoModeFile(filepath.Join(secretsDir, "crypto_mode"), crypto.ModeAES); err != nil {
		t.Fatalf("write crypto_mode: %v", err)
	}

	dbPath := filepath.Join(dataDir, "credentials.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("OpenForTesting: %v", err)
	}
	defer s.Close()

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{
		SecretsDir: secretsDir,
	}, s)
	if err != nil {
		t.Fatalf("SelfInit: %v", err)
	}

	// Suppress the startup auto-DEK-rotate (gate is dek_rotated_at>0 within
	// rotation.period): this runbook is about KEK S1 recovery, and a DEK
	// rotation would add pending_dek noise to the crash state we construct.
	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatalf("GetKeyMetadata after SelfInit: %v", err)
	}
	km.DekRotatedAt = time.Now().Unix()
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatalf("UpdateKeyMetadata (set dek_rotated_at): %v", err)
	}

	// Seed bulk rows encrypted under the active DEK (they must decrypt during
	// Phase A of any DEK rotation, hence valid ciphertext, though this test
	// never performs a DEK rotation).
	seedCredentialRows(t, s, mgr.Current().DEK.Bytes(), seedBulkCreds)
}
// seedCredentialRows bulk-inserts n credentials in a single transaction.
func seedCredentialRows(t *testing.T, s *store.Store, dek []byte, n int) {
	t.Helper()
	db := s.AdminDB()
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin seed tx: %v", err)
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO credentials
		(user_id, api_base, key_tag, proxy_key, api_key_cipher, auth_type,
		 row_version, kek_version, dek_version, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?)`)
	if err != nil {
		t.Fatalf("prepare seed stmt: %v", err)
	}
	defer stmt.Close()
	now := time.Now().Unix()
	for i := 0; i < n; i++ {
		uid := fmt.Sprintf("bulk-%05d", i)
		ct, err := crypto.EncryptCredential(crypto.ModeAES, dek, []byte("sk-bulk-"+uid))
		if err != nil {
			t.Fatalf("encrypt seed row %d: %v", i, err)
		}
		if _, err := stmt.Exec(uid, "https://bulk.example.com/"+uid, "default", "cr_pk_bulk"+uid, ct, "openai", now, now); err != nil {
			t.Fatalf("insert seed row %d: %v", i, err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit seed tx: %v", err)
	}
}

// startServer writes a per-round config, spawns the binary, waits for
// /v1/health, and returns the cmd, the admin base address, and a cleanup func
// (graceful SIGINT → SIGKILL group).
func startServer(t *testing.T, binPath, secretsDir, dataDir string) (*exec.Cmd, string, func()) {
	t.Helper()
	proxyPort := freePort(t)
	adminPort := freePort(t)
	tmpDir := filepath.Dir(dataDir)
	configPath := writeRunbookConfig(t, tmpDir, secretsDir, dataDir, proxyPort, adminPort)

	cmd := exec.Command(binPath, "-config", configPath)
	cmd.Dir = tmpDir
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	cmd.SysProcAttr = procGroupAttrs()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start credential-router: %v", err)
	}
	runbookSrvLog.stdout = &stdout
	runbookSrvLog.stderr = &stderr

	adminAddr := fmt.Sprintf("127.0.0.1:%d", adminPort)
	waitForHealth(t, adminAddr, 5*time.Second)

	cleanup := func() {
		stopRouterProcess(cmd)
	}
	return cmd, adminAddr, cleanup
}

// runbookConfig is the minimal YAML the binary needs; config.Load fills in
// defaults for everything else.
type runbookConfig struct {
	Server struct {
		BindAddress string `yaml:"bind_address"`
	} `yaml:"server"`
	DataDir   string `yaml:"data_dir"`
	BackupDir string `yaml:"backup_dir"`
	SSRF      struct {
		AllowedHosts []string `yaml:"allowed_hosts,omitempty"`
	} `yaml:"ssrf"`
	Admin struct {
		Addr string `yaml:"addr"`
	} `yaml:"admin"`
}

func writeRunbookConfig(t *testing.T, tmpDir, secretsDir, dataDir string, proxyPort, adminPort int) string {
	t.Helper()
	var cfg runbookConfig
	cfg.Server.BindAddress = fmt.Sprintf("127.0.0.1:%d", proxyPort)
	cfg.DataDir = dataDir
	cfg.BackupDir = filepath.Join(tmpDir, "backups")
	cfg.Admin.Addr = fmt.Sprintf("127.0.0.1:%d", adminPort)
	cfg.SSRF.AllowedHosts = []string{
		"127.0.0.1",
		"localhost",
		"api.example.com",
		"bulk.example.com",
	}
	data, err := yaml.Marshal(&cfg)
	if err != nil {
		t.Fatalf("marshal config: %v", err)
	}
	path := filepath.Join(tmpDir, "config.yaml")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return path
}

// waitForHealth polls GET /v1/health until it returns 200.
func waitForHealth(t *testing.T, adminAddr string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		resp, err := http.Get("http://" + adminAddr + "/v1/health")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("admin /v1/health not OK at %s within %s\nSTDOUT:\n%s\nSTDERR:\n%s",
		adminAddr, timeout, runbookSrvLog.stdout.String(), runbookSrvLog.stderr.String())
}

// waitForIdle polls /v1/keystore/status until neither KEK nor DEK rotation is
// pending.
func waitForIdle(t *testing.T, adminAddr string, timeout time.Duration) {
	t.Helper()
	var lastPK, lastPD int64
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		pk, pd, ok := statusPending(adminAddr)
		if ok {
			lastPK, lastPD = pk, pd
			if pk == 0 && pd == 0 {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("keystore not idle at %s within %s (last pending_kek=%d pending_dek=%d)\nSTDOUT:\n%s\nSTDERR:\n%s",
		adminAddr, timeout, lastPK, lastPD, runbookSrvLog.stdout.String(), runbookSrvLog.stderr.String())
}

// statusPending returns the pending_kek_version / pending_dek_version from
// /v1/keystore/status, and whether the status was readable. Reads use the
// store's read pool, so they succeed concurrently with an in-flight rotation.
func statusPending(adminAddr string) (pendingKEK, pendingDEK int64, ok bool) {
	resp, err := http.Get("http://" + adminAddr + "/v1/keystore/status")
	if err != nil {
		return 0, 0, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, 0, false
	}
	var parsed struct {
		Data struct {
			PendingKekVersion int64 `json:"pending_kek_version"`
			PendingDekVersion int64 `json:"pending_dek_version"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return 0, 0, false
	}
	return parsed.Data.PendingKekVersion, parsed.Data.PendingDekVersion, true
}

// waitForPendingKEK polls the status endpoint until the KEK rotation has been
// recorded in the DB (BeginKEKRotation committed pending_kek_version>0),
// returning false if the rotation completed before it could be observed.
func waitForPendingKEK(t *testing.T, timeout time.Duration) bool {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		pk, _, ok := statusPending(runbookAdminAddr)
		if ok && pk > 0 {
			return true
		}
		time.Sleep(1 * time.Millisecond)
	}
	return false
}

// randomKillMidRotation SIGKILLs the server while the KEK rotation is in
// flight. It first waits until the DB shows pending_kek_version>0 (Begin has
// committed), then sleeps a random delay in [minMs,maxMs] and kills. Waiting
// for the DB signal guarantees the kill lands after Begin's metadata update —
// the state recovery Case 4 (KEK S1 forward) is built to resolve — and the
// delay randomizes exactly where in the bulk UPDATE the process dies.
func randomKillMidRotation(t *testing.T, pid int, serverCmd *exec.Cmd, minMs, maxMs int) {
	t.Helper()
	if !waitForPendingKEK(t, 5*time.Second) {
		// Rotation completed before we could observe it (fast path / long
		// delay variant). The kill then lands post-completion, and recovery
		// is Case 1 (clean) — still a valid crash-restart exercise.
		t.Log("pending_kek_version was not observed; rotation completed before SIGKILL")
	}
	delay := minMs
	if maxMs > minMs {
		delay = minMs + rand.Intn(maxMs-minMs+1)
	}
	if delay > 0 {
		time.Sleep(time.Duration(delay) * time.Millisecond)
	}
	if err := serverCmd.Process.Kill(); err != nil {
		t.Fatalf("kill server pid=%d: %v", serverCmd.Process.Pid, err)
	}
	waitForExit(t, serverCmd)
}

// waitForExit waits for the killed process to be reaped.
func waitForExit(t *testing.T, cmd *exec.Cmd) {
	t.Helper()
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatalf("process %d did not exit after SIGKILL", cmd.Process.Pid)
	}
}

// httpPost performs an HTTP POST with a JSON body, failing the test on
// transport errors.
func httpPost(t *testing.T, url, body string) (int, string) {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

// httpGet performs an HTTP GET, failing the test on transport errors.
func httpGet(t *testing.T, url string) (int, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

// postCredential creates the runbook credential via the admin API and records
// the server-minted proxy_key in runbookProxyKey for later read-back.
func postCredential(t *testing.T, adminAddr string) {
	t.Helper()
	body := fmt.Sprintf(`{"user_id":%q,"api_base":%q,"key_tag":%q,"auth_type":"openai","api_key":%q}`,
		runbookUser, runbookURL, runbookKeyTag, runbookAPIKey)
	status, respBody := httpPost(t, "http://"+adminAddr+"/v1/credentials", body)
	if status/100 != 2 {
		t.Fatalf("POST /v1/credentials: status=%d body=%s", status, respBody)
	}
	var resp struct {
		Data struct {
			ProxyKey string `json:"proxy_key"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(respBody), &resp); err != nil {
		t.Fatalf("parse create response: %v body=%s", err, respBody)
	}
	if resp.Data.ProxyKey == "" {
		t.Fatalf("create response missing proxy_key: body=%s", respBody)
	}
	runbookProxyKey = resp.Data.ProxyKey
}

// verifyStatusPostRecovery asserts the post-crash restart promoted the KEK to
// v2 with no pending KEK/DEK rotation.
func verifyStatusPostRecovery(t *testing.T, adminAddr string) {
	t.Helper()
	status, body := httpGet(t, "http://"+adminAddr+"/v1/keystore/status")
	if status != http.StatusOK {
		t.Fatalf("GET /v1/keystore/status: status=%d body=%s", status, body)
	}
	var resp struct {
		Data struct {
			ActiveKekVersion  int64 `json:"active_kek_version"`
			PendingKekVersion int64 `json:"pending_kek_version"`
			PendingDekVersion int64 `json:"pending_dek_version"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("parse keystore status: %v body=%s", err, body)
	}
	if resp.Data.ActiveKekVersion != 2 {
		t.Fatalf("active_kek_version=%d, want 2 (rotation must have been promoted) body=%s",
			resp.Data.ActiveKekVersion, body)
	}
	if resp.Data.PendingKekVersion != 0 {
		t.Fatalf("pending_kek_version=%d, want 0 body=%s", resp.Data.PendingKekVersion, body)
	}
	if resp.Data.PendingDekVersion != 0 {
		t.Fatalf("pending_dek_version=%d, want 0 body=%s", resp.Data.PendingDekVersion, body)
	}
}

// verifyCredentialDecrypt asserts the runbook credential is readable with its
// api_key decrypted back to plaintext, and (post-recovery) is stamped with the
// promoted KEK version. The credential is addressed by the proxy_key captured
// at create time (runbookProxyKey).
func verifyCredentialDecrypt(t *testing.T, adminAddr string, wantKekVersion int64) {
	t.Helper()
	if runbookProxyKey == "" {
		t.Fatal("verifyCredentialDecrypt: runbookProxyKey not set (postCredential must run first)")
	}
	status, body := httpGet(t, "http://"+adminAddr+"/v1/credentials/"+runbookProxyKey)
	if status != http.StatusOK {
		t.Fatalf("GET /v1/credentials: status=%d body=%s", status, body)
	}
	var resp struct {
		Data struct {
			APIKey     string `json:"api_key"`
			KekVersion int64  `json:"kek_version"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("parse credential: %v body=%s", err, body)
	}
	if resp.Data.APIKey != runbookAPIKey {
		t.Fatalf("api_key=%q, want %q (wrapped_dek unwrap failed post-recovery?) body=%s",
			resp.Data.APIKey, runbookAPIKey, body)
	}
	if wantKekVersion > 0 && resp.Data.KekVersion != wantKekVersion {
		t.Fatalf("credential kek_version=%d, want %d body=%s",
			resp.Data.KekVersion, wantKekVersion, body)
	}
}
