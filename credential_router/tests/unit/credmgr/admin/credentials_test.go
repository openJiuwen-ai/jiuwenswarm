//go:build cgo

package admin_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"credential_router/internal/credmgr/admin"
	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/cache"
	"credential_router/internal/platform"
	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/proxy/ssrf"
	"credential_router/internal/credmgr"
)

// Local mirrors of the admin package's wire structs. External tests decode
// the JSON envelope into these; the JSON tags match the production structs.
type credentialRequest struct {
	UserID   string `json:"user_id"`
	APIBase  string `json:"api_base"`
	KeyTag   string `json:"key_tag"`
	APIKey   string `json:"api_key"`
	AuthType string `json:"auth_type"`
}

type credentialResponse struct {
	UserID       string `json:"user_id"`
	APIBase      string `json:"api_base"`
	KeyTag       string `json:"key_tag"`
	AuthType     string `json:"auth_type"`
	APIKey       string `json:"api_key,omitempty"`
	KekVersion   int64  `json:"kek_version"`
	DekVersion   int64  `json:"dek_version"`
	CreatedAt    int64  `json:"created_at"`
	UpdatedAt    int64  `json:"updated_at"`
	ProxyKey     string `json:"proxy_key"`
	ProxyAddress string `json:"proxy_address"`
}

type adminHarness struct {
	server     *admin.Server
	router     http.Handler
	store      *store.Store
	mgr        *keystore.Manager
	rot        *keystore.Rotator
	getter     *credmgr.CredMgr
	ccg        *cache.CachedCredentialGetter
	cache      cache.CredentialCache
	cfg        platform.Config
	secretsDir string
	cleanup    func()
}

func newAdminHarness(t *testing.T) *adminHarness {
	return newAdminHarnessWithAdminConns(t, 1)
}

// newAdminHarnessWithAdminConns mirrors newAdminHarness but lets callers
// widen the store adminDB pool so we can exercise the AdminMaxConns > 1
// path that the previous LockWrite had been hiding.
func newAdminHarnessWithAdminConns(t *testing.T, adminMaxConns int) *adminHarness {
	t.Helper()
	secretsDir := t.TempDir()
	dataDir := t.TempDir()
	backupDir := filepath.Join(dataDir, "backups")

	var s1 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}
	for _, f := range []struct {
		name string
		data []byte
	}{
		{"s1.bin.1", s1[:]},
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, f.name), f.data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := keystore.WriteCryptoModeFile(filepath.Join(secretsDir, "crypto_mode"), crypto.ModeAES); err != nil {
		t.Fatal(err)
	}

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenWithConfig(store.OpenConfig{Path: dbPath, AdminMaxConns: adminMaxConns})
	if err != nil {
		t.Fatal(err)
	}
	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{SecretsDir: secretsDir}, s)
	if err != nil {
		t.Fatal(err)
	}
	cc := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 1000, TombstoneTTL: 0})
	getter := credmgr.NewCredMgr(s, mgr)
	ccg := cache.NewCachedCredentialGetter(cc, getter)
	bm, err := backup.NewBackupManager(backup.BackupConfig{BackupDir: backupDir}, s)
	if err != nil {
		t.Fatal(err)
	}
	rot := keystore.NewRotator(mgr, s, bm, secretsDir)

	cfg := platform.Default()
	server := admin.NewServer(cfg.Admin, cfg.SSRF, cfg.Server, cfg.Rotation, cfg.Recovery, s, ccg, rot)
	server.SetPolicyFactoryForTesting(func() *ssrf.URLPolicy { return ssrf.TestPolicy() })
	router := server.Routes()

	return &adminHarness{
		server:     server,
		router:     router,
		store:      s,
		mgr:        mgr,
		rot:        rot,
		getter:     getter,
		ccg:        ccg,
		cache:      cc,
		cfg:        cfg,
		secretsDir: secretsDir,
		cleanup:    func() { _ = s.Close() },
	}
}

func (h *adminHarness) do(t *testing.T, method, path string, body interface{}) *httptest.ResponseRecorder {
	return h.doWithHeaders(t, method, path, body, nil)
}

func (h *adminHarness) doWithHeaders(t *testing.T, method, path string, body interface{}, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req := httptest.NewRequest(method, path, &buf)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.router.ServeHTTP(rec, req)
	return rec
}

// createCredential POSTs a credential and returns the decoded response
// (which carries the server-minted proxy_key used in all follow-up paths).
func createCredential(t *testing.T, h *adminHarness, userID, apiBase, keyTag, apiKey, authType string) credentialResponse {
	t.Helper()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": userID, "api_base": apiBase, "key_tag": keyTag,
		"api_key": apiKey, "auth_type": authType,
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("create: status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec.Body.Bytes(), &resp)
	if resp.ProxyKey == "" {
		t.Fatal("create response missing proxy_key")
	}
	return resp
}

func decodeEnvelope(t *testing.T, body []byte, target interface{}) {
	t.Helper()
	var env struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatalf("decode envelope: %v\nbody=%s", err, body)
	}
	if err := json.Unmarshal(env.Data, target); err != nil {
		t.Fatalf("decode data: %v\ndata=%s", err, env.Data)
	}
}

func envelopeErrorCode(t *testing.T, body []byte) string {
	t.Helper()
	var env struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatalf("decode error envelope: %v\nbody=%s", err, body)
	}
	return env.Error.Code
}

// ── createCredential ───────────────────────────────────────────────────

func TestCreateCredentialSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec.Body.Bytes(), &resp)
	if resp.UserID != "u1" || resp.AuthType != "openai" {
		t.Errorf("unexpected response: %+v", resp)
	}
}

func TestCreateCredentialInvalidJSON(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	req := httptest.NewRequest("POST", "/v1/credentials", strings.NewReader("not json"))
	rec := httptest.NewRecorder()
	h.router.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status=%d, want 400", rec.Code)
	}
}

func TestCreateCredentialInvalidUserID(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "bad space!", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk", "auth_type": "openai",
	})
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status=%d, want 400", rec.Code)
	}
}

func TestCreateCredentialMissingAPIKey(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"auth_type": "openai",
	})
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status=%d, want 400", rec.Code)
	}
}

func TestCreateCredentialAPIKeyTooLong(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	maxLen := h.cfg.Admin.Validation.APIKeyMaxLen
	apiKey := strings.Repeat("x", maxLen+1)
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id":   "u1",
		"api_base":  "https://api.example.com",
		"key_tag":   "default",
		"api_key":   apiKey,
		"auth_type": "openai",
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400, body=%s", rec.Code, rec.Body.String())
	}
	rec = h.do(t, "GET", "/v1/credentials?limit=200", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status=%d", rec.Code)
	}
}

func TestUpdateCredentialAPIKeyTooLong(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	created := createCredential(t, h, "u1", "https://api.example.com", "default", "sk-original", "openai")
	maxLen := h.cfg.Admin.Validation.APIKeyMaxLen
	apiKey := strings.Repeat("x", maxLen+1)
	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey, map[string]string{
		"api_key": apiKey,
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d, want 400, body=%s", rec.Code, rec.Body.String())
	}
	rec = h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("get status=%d", rec.Code)
	}
	var resp struct {
		Data struct {
			APIKey string `json:"api_key"`
		} `json:"data"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Data.APIKey != "sk-original" {
		t.Errorf("api_key was modified despite 400 response: got %q", resp.Data.APIKey)
	}
}

func TestCreateCredentialBodyTooLarge(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	body := strings.NewReader(`{"user_id":"u1","api_base":"https://api.example.com","key_tag":"default","api_key":"sk","auth_type":"openai"}`)
	req := httptest.NewRequest("POST", "/v1/credentials", body)
	req.Header.Set("Content-Type", "application/json")
	req.ContentLength = h.cfg.Server.MaxRequestBytes + 1
	rec := httptest.NewRecorder()
	h.router.ServeHTTP(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d, want 413, body=%s", rec.Code, rec.Body.String())
	}
}

func TestCreateCredentialDuplicate(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	body := map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk", "auth_type": "openai",
	}
	if r := h.do(t, "POST", "/v1/credentials", body); r.Code != http.StatusCreated {
		t.Fatalf("first insert: status=%d", r.Code)
	}
	rec := h.do(t, "POST", "/v1/credentials", body)
	if rec.Code != http.StatusConflict {
		t.Errorf("duplicate status=%d, want 409", rec.Code)
	}
}

// ── getCredential ───────────────────────────────────────────────────────

func TestGetCredentialSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	created := createCredential(t, h, "u1", "https://api.example.com", "default", "sk-abc", "openai")
	rec := h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec.Body.Bytes(), &resp)
	if resp.APIKey != "sk-abc" {
		t.Errorf("api_key=%q, want sk-abc", resp.APIKey)
	}
	if resp.AuthType != "openai" {
		t.Errorf("auth_type=%q, want openai", resp.AuthType)
	}
}

func TestCreateAndReadUserIDEcho(t *testing.T) {
	cases := []struct {
		userID string
		apiKey string
	}{
		{"alice", "sk-alice-001"},
		{"bob", "sk-bob-002"},
		{"user-with-dashes", "sk-dashes"},
		{"USER_UPPER_CASE", "sk-upper"},
	}
	for _, c := range cases {
		t.Run(c.userID, func(t *testing.T) {
			h := newAdminHarness(t)
			defer h.cleanup()

			createResp := h.do(t, "POST", "/v1/credentials", map[string]string{
				"user_id": c.userID, "api_base": "https://api.example.com", "key_tag": "default",
				"api_key": c.apiKey, "auth_type": "openai",
			})
			if createResp.Code != http.StatusCreated {
				t.Fatalf("insert: status=%d body=%s", createResp.Code, createResp.Body.String())
			}
			var created credentialResponse
			decodeEnvelope(t, createResp.Body.Bytes(), &created)
			if created.UserID != c.userID {
				t.Errorf("create echo user_id=%q want %q", created.UserID, c.userID)
			}

			getResp := h.do(t, "GET",
				"/v1/credentials/"+created.ProxyKey, nil)
			if getResp.Code != http.StatusOK {
				t.Fatalf("GET: status=%d body=%s", getResp.Code, getResp.Body.String())
			}
			var got credentialResponse
			decodeEnvelope(t, getResp.Body.Bytes(), &got)
			if got.UserID != c.userID {
				t.Errorf("GET user_id=%q want %q (userID not round-tripped)", got.UserID, c.userID)
			}
			if got.APIKey != c.apiKey {
				t.Errorf("GET api_key=%q want %q", got.APIKey, c.apiKey)
			}
		})
	}
}

func TestGetCredentialNotFound(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "GET", "/v1/credentials/missing/https%3A%2F%2Fapi.example.com/default", nil)
	if rec.Code != http.StatusNotFound {
		t.Errorf("status=%d, want 404", rec.Code)
	}
}

// ── updateCredential ───────────────────────────────────────────────────

func TestUpdateCredentialSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-old", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey, map[string]interface{}{
		"api_key": "sk-new", "auth_type": "openai",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestUpdateCredentialNotFound(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "PUT", "/v1/credentials/abc", map[string]string{"api_key": "x"})
	if rec.Code != http.StatusNotFound {
		t.Errorf("status=%d, want 404", rec.Code)
	}
}

// ── deleteCredential ───────────────────────────────────────────────────

func TestDeleteCredentialSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rec := h.do(t, "DELETE", "/v1/credentials/"+created.ProxyKey, nil)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

// ── body limit ─────────────────────────────────────────────────────────

func TestBodyTooLarge(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	big := strings.Repeat("x", int(h.cfg.Server.MaxRequestBytes)+1)
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": big, "auth_type": "openai",
	})
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status=%d, want 413 for oversize body", rec.Code)
	}
}

// TestCreateAfterDelete verifies that re-creating a credential with the same
// (user_id, api_base, key_tag) after it was deleted succeeds: DELETE removes
// the row, so a later POST is a fresh insert (overwrite_tombstone is gone).
func TestCreateAfterDelete(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]any{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)
	h.do(t, "DELETE", "/v1/credentials/"+created.ProxyKey, nil)

	r2 := h.do(t, "POST", "/v1/credentials", map[string]any{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk2", "auth_type": "openai",
	})
	if r2.Code != http.StatusCreated {
		t.Errorf("status=%d, want 201 (fresh insert after delete)", r2.Code)
	}
}

func TestDeleteSetsTombstone(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)
	h.do(t, "DELETE", "/v1/credentials/"+created.ProxyKey, nil)

	r2 := h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if r2.Code != http.StatusNotFound {
		t.Errorf("status=%d, want 404 after delete", r2.Code)
	}
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

// TestCreatePopulatesCache verifies that after a successful create, the cache
// holds the entry so a subsequent GET hits cache (write-through).
func TestCreatePopulatesCache(t *testing.T) {
	h := newAdminHarness(t)
	userID, apiBase, keyTag := "u-create-cache", "https://api.example.com", "default"

	rr := h.do(t, "POST", "/v1/credentials", credentialRequest{
		UserID: userID, APIBase: apiBase, KeyTag: keyTag,
		APIKey: "secret", AuthType: "openai",
	})
	if rr.Code != http.StatusCreated {
		t.Fatalf("create status=%d body=%s, want 201", rr.Code, rr.Body.String())
	}
	var created credentialResponse
	decodeEnvelope(t, rr.Body.Bytes(), &created)

	got, _, err := h.ccg.GetCredentialByProxyKey(created.ProxyKey)
	if err != nil {
		t.Fatalf("cache.Get after create: %v", err)
	}
	if got == "" {
		t.Fatal("cache.Get after create: empty apiKey")
	}
}

// TestUpdateUpdatesCache verifies that an update writes through to cache (item 2).
func TestUpdateUpdatesCache(t *testing.T) {
	h := newAdminHarness(t)
	userID, apiBase, keyTag := "u-update-cache", "https://api.example.com", "default"

	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": userID, "api_base": apiBase, "key_tag": keyTag,
		"api_key": "secret", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rr := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey,
		map[string]interface{}{"api_key": "secret2", "auth_type": "openai"})
	if rr.Code != http.StatusOK {
		t.Fatalf("update status=%d body=%s", rr.Code, rr.Body.String())
	}

	got, _, err := h.ccg.GetCredentialByProxyKey(created.ProxyKey)
	if err != nil {
		t.Fatalf("cache.Get after update: %v", err)
	}
	if got != "secret2" {
		t.Errorf("cache.Get after update = %q, want %q", got, "secret2")
	}
}

// TestCreateCredentialNormalizesAPIBase verifies item 3 (server-side normalize):
// a api_base with a trailing slash is stored without the trailing slash.
func TestCreateCredentialNormalizesAPIBase(t *testing.T) {
	h := newAdminHarness(t)
	rr := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com/", "key_tag": "default",
		"api_key": "secret", "auth_type": "openai",
	})
	if rr.Code != http.StatusCreated {
		t.Fatalf("create with trailing slash: status=%d body=%s, want 201", rr.Code, rr.Body.String())
	}
	var created credentialResponse
	decodeEnvelope(t, rr.Body.Bytes(), &created)
	if created.APIBase != "https://api.example.com" {
		t.Errorf("stored api_base = %q, want %q (trailing slash stripped)", created.APIBase, "https://api.example.com")
	}
}

// TestCreateCredentialWithTrailingSlashCollision verifies item 3 uniqueness:
// creating with URL ending in "/" and again without must yield 409 conflict.
func TestCreateCredentialWithTrailingSlashCollision(t *testing.T) {
	h := newAdminHarness(t)
	rr := h.do(t, "POST", "/v1/credentials", credentialRequest{
		UserID: "u1", APIBase: "https://api.example.com/", KeyTag: "default",
		APIKey: "secret", AuthType: "openai",
	})
	if rr.Code != http.StatusCreated {
		t.Fatalf("first create: status=%d body=%s", rr.Code, rr.Body.String())
	}

	rr = h.do(t, "POST", "/v1/credentials", credentialRequest{
		UserID: "u1", APIBase: "https://api.example.com", KeyTag: "default",
		APIKey: "secret2", AuthType: "openai",
	})
	if rr.Code != http.StatusConflict {
		t.Fatalf("duplicate normalized URL: status=%d body=%s, want 409", rr.Code, rr.Body.String())
	}
}

// TestCreateCredentialWriteThroughFailReturns500 verifies the write-through
// failure path: when the cache write-through fails after the SQLite commit,
// the handler returns 500 but the row persists — a subsequent GET must still
// succeed (self-healing read path).
func TestCreateCredentialWriteThroughFailReturns500(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	h.ccg.SetWriteThroughFailForTesting(true)
	defer h.ccg.SetWriteThroughFailForTesting(false)

	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	})
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status=%d body=%s, want 500", rec.Code, rec.Body.String())
	}

	cred, err := h.store.GetCredentialByUserURLTag(context.Background(), "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("read back: %v", err)
	}

	rec2 := h.do(t, "GET", "/v1/credentials/"+cred.ProxyKey, nil)
	if rec2.Code != http.StatusOK {
		t.Fatalf("GET after 500: status=%d body=%s, want 200 (DB committed)", rec2.Code, rec2.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec2.Body.Bytes(), &resp)
	if resp.APIKey != "sk-abc" {
		t.Errorf("api_key=%q, want sk-abc (row committed despite cache failure)", resp.APIKey)
	}
}

// TestUpdateCredentialRequiresAtLeastOneField — PUT with neither api_key nor
// auth_type is a no-op; rejected with 400. At least one field must be
// present.
func TestUpdateCredentialRequiresAtLeastOneField(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-old", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey,
		map[string]interface{}{})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s, want 400", rec.Code, rec.Body.String())
	}
	if code := envelopeErrorCode(t, rec.Body.Bytes()); code != "bad_request" {
		t.Errorf("error code=%q, want bad_request", code)
	}
}

// TestUpdateCredentialPartialAuthTypeOnly — PUT with only auth_type must
// preserve existing api_key_cipher (still decryptable).
func TestUpdateCredentialPartialAuthTypeOnly(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-original", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey,
		map[string]interface{}{"auth_type": "anthropic"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec.Body.Bytes(), &resp)
	if resp.AuthType != "anthropic" {
		t.Errorf("auth_type=%q, want anthropic", resp.AuthType)
	}
	if resp.APIKey != "" {
		t.Errorf("api_key=%q, want empty (caller did not provide new value)", resp.APIKey)
	}

	// Verify DB by GET: api_key must still decrypt to the original plaintext.
	getRec := h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if getRec.Code != http.StatusOK {
		t.Fatalf("GET status=%d body=%s", getRec.Code, getRec.Body.String())
	}
	var got credentialResponse
	decodeEnvelope(t, getRec.Body.Bytes(), &got)
	if got.APIKey != "sk-original" {
		t.Errorf("api_key=%q, want sk-original (preserved)", got.APIKey)
	}
	if got.AuthType != "anthropic" {
		t.Errorf("auth_type=%q, want anthropic", got.AuthType)
	}
}

// TestUpdateCredentialPartialAPIKeyOnly — PUT with only api_key must
// preserve existing auth_type.
func TestUpdateCredentialPartialAPIKeyOnly(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-old", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey,
		map[string]interface{}{"api_key": "sk-new"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}

	// PUT does not echo plaintext; verify the new key via GET.
	getRec := h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if getRec.Code != http.StatusOK {
		t.Fatalf("GET after PUT: status=%d body=%s", getRec.Code, getRec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, getRec.Body.Bytes(), &resp)
	if resp.APIKey != "sk-new" {
		t.Errorf("api_key=%q, want sk-new", resp.APIKey)
	}
	if resp.AuthType != "openai" {
		t.Errorf("auth_type=%q, want openai (preserved)", resp.AuthType)
	}
}

// TestGetCredentialMalformedProxyKey404 verifies getCredential's miss path:
// any single-segment path is treated as a raw proxy_key and looked up
// directly (no b64 decode anymore). Neither a garbage string nor a decodable
// single-part token matches a stored proxy_key, so both yield 404.
func TestGetCredentialMalformedProxyKey404(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	rec := h.do(t, "GET", "/v1/credentials/"+url.PathEscape("%%%"), nil)
	if rec.Code != http.StatusNotFound {
		t.Errorf("garbage key: status=%d body=%s, want 404", rec.Code, rec.Body.String())
	}

	singlePart := base64.RawURLEncoding.EncodeToString([]byte("u1"))
	rec = h.do(t, "GET", "/v1/credentials/"+singlePart, nil)
	if rec.Code != http.StatusNotFound {
		t.Errorf("non-proxy-key segment: status=%d body=%s, want 404", rec.Code, rec.Body.String())
	}
}

// TestGetCredentialValidKeyNotFound404 verifies the handler-level 404: a
// well-formed proxy_key for a never-inserted credential must return 404.
func TestGetCredentialValidKeyNotFound404(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	rec := h.do(t, "GET", "/v1/credentials/"+store.GenerateProxyKey(), nil)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d body=%s, want 404", rec.Code, rec.Body.String())
	}
	if code := envelopeErrorCode(t, rec.Body.Bytes()); code != "not_found" {
		t.Errorf("error code=%q, want not_found", code)
	}
}

// ── createCredential writes snap.DekVersion ────────────────────────

func TestCreateCredentialWritesSnapDekVersion(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	snap := h.mgr.Current()
	wantDek := int64(snap.DekVersion)
	wantKek := int64(snap.KekVersion)

	if r := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	}); r.Code != http.StatusCreated {
		t.Fatalf("create: status=%d body=%s", r.Code, r.Body.String())
	}

	cred, err := h.store.GetCredentialByUserURLTag(context.Background(), "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if cred.DekVersion != wantDek {
		t.Errorf("DekVersion=%d, want %d", cred.DekVersion, wantDek)
	}
	if cred.KekVersion != wantKek {
		t.Errorf("KekVersion=%d, want %d", cred.KekVersion, wantKek)
	}
}

func TestUpdateCredentialWritesSnapDekVersion(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	r1 := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-old", "auth_type": "openai",
	})
	var created credentialResponse
	decodeEnvelope(t, r1.Body.Bytes(), &created)

	snap := h.mgr.Current()
	wantDek := int64(snap.DekVersion)
	wantKek := int64(snap.KekVersion)

	r2 := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey,
		map[string]interface{}{"api_key": "sk-new", "auth_type": "openai"})
	if r2.Code != http.StatusOK {
		t.Fatalf("update: status=%d body=%s", r2.Code, r2.Body.String())
	}

	cred, err := h.store.GetCredentialByUserURLTag(context.Background(), "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if cred.DekVersion != wantDek {
		t.Errorf("DekVersion=%d, want %d", cred.DekVersion, wantDek)
	}
	if cred.KekVersion != wantKek {
		t.Errorf("KekVersion=%d, want %d", cred.KekVersion, wantKek)
	}
}

// ── deleteCredential hides cache entry via tombstone ──────────────

func TestDeleteCredentialHidesCacheEntry(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	if r := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	}); r.Code != http.StatusCreated {
		t.Fatalf("create: status=%d body=%s", r.Code, r.Body.String())
	}

	cred, err := h.store.GetCredentialByUserURLTag(context.Background(), "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("read back: %v", err)
	}

	if _, _, err := h.ccg.GetCredentialByProxyKey(cred.ProxyKey); err != nil {
		t.Fatalf("pre-delete cache check: %v", err)
	}

	del := h.doWithHeaders(t, "DELETE", "/v1/credentials/"+cred.ProxyKey, nil, nil)
	if del.Code != http.StatusNoContent {
		t.Fatalf("delete: status=%d body=%s", del.Code, del.Body.String())
	}

	_, _, err = h.ccg.GetCredentialByProxyKey(cred.ProxyKey)
	if err == nil {
		t.Fatal("post-delete cache check returned nil; expected ErrCredentialNotFound")
	}
	if !errors.Is(err, credmgr.ErrCredentialNotFound) {
		t.Errorf("post-delete err=%v, want credmgr.ErrCredentialNotFound", err)
	}
}

// TestAdminConcurrentUpdateSameRowRecovers exercises AdminMaxConns=2
// plus the handler-level 3-attempt row_version retry: many goroutines
// PUT the same row concurrently. Exactly one wins the first round; the
// rest hit the server-internal optimistic lock, re-SELECT, re-merge, and
// retry. After all complete, the row exists with the latest payload and
// row_version equals the number of successful updates.
//
// target is bounded to maxUpdateAttempts (3 retries => 4 contenders
// worst-case can all land: 1 winner in round 1 + 1 winner in each of
// the 3 retry rounds). Higher values flaked under parallel-test load
// because the worst-case race leaves more contenders than retry slots.
func TestAdminConcurrentUpdateSameRowRecovers(t *testing.T) {
	h := newAdminHarnessWithAdminConns(t, 2)
	defer h.cleanup()

	const target = 4
	create := h.do(t, "POST", "/v1/credentials", &credentialRequest{
		UserID:   "u0",
		APIBase:  "https://api.example.com",
		KeyTag:   "default",
		APIKey:   "seed-key",
		AuthType: "openai",
	})
	if create.Code != http.StatusCreated {
		t.Fatalf("seed create: status=%d body=%s", create.Code, create.Body.String())
	}
	var seed credentialResponse
	decodeEnvelope(t, create.Body.Bytes(), &seed)
	seedCred, err := h.store.GetCredentialByProxyKey(context.Background(), seed.ProxyKey)
	if err != nil {
		t.Fatalf("seed read back: %v", err)
	}
	seedRowVersion := seedCred.RowVersion

	type result struct {
		idx       int
		finalCode int
	}
	results := make(chan result, target)
	for i := 0; i < target; i++ {
		i := i
		go func() {
			upd := h.do(t, "PUT", "/v1/credentials/"+seed.ProxyKey,
				&credentialRequest{
					APIKey: "k-" + itoa(int64(i)),
				},
			)
			results <- result{idx: i, finalCode: upd.Code}
		}()
	}

	var wins int
	for i := 0; i < target; i++ {
		r := <-results
		if r.finalCode == http.StatusOK {
			wins++
		}
	}
	if wins != target {
		t.Fatalf("expected all %d concurrent updates to eventually succeed via retry, got %d", target, wins)
	}

	final, err := h.store.GetCredentialByProxyKey(context.Background(), seed.ProxyKey)
	if err != nil {
		t.Fatalf("final store read: %v", err)
	}
	if final.RowVersion != seedRowVersion+int64(target) {
		t.Errorf("final row_version = %d, want %d", final.RowVersion, seedRowVersion+int64(target))
	}
}

// TestAdminConcurrentCreateDistinctKeys verifies that AdminMaxConns=2
// allows many concurrent create requests on different keys to land
// without cross-talk, lost inserts, or duplicate row_version collisions.
func TestAdminConcurrentCreateDistinctKeys(t *testing.T) {
	h := newAdminHarnessWithAdminConns(t, 2)
	defer h.cleanup()

	type createdResult struct {
		code     int
		proxyKey string
	}
	const n = 12
	results := make(chan createdResult, n)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			c := h.do(t, "POST", "/v1/credentials", &credentialRequest{
				UserID:   "u-" + itoa(int64(i)),
				APIBase:  "https://api.example.com",
				KeyTag:   "default",
				APIKey:   "key-" + itoa(int64(i)),
				AuthType: "openai",
			})
			var created credentialResponse
			if c.Code == http.StatusCreated {
				decodeEnvelope(t, c.Body.Bytes(), &created)
			}
			results <- createdResult{code: c.Code, proxyKey: created.ProxyKey}
		}()
	}
	proxyKeys := make([]string, n)
	for i := 0; i < n; i++ {
		r := <-results
		if r.code != http.StatusCreated {
			t.Errorf("concurrent create #%d: status=%d", i, r.code)
			continue
		}
		proxyKeys[i] = r.proxyKey
	}
	rowVersionSeen := make(map[int64]int)
	for i := 0; i < n; i++ {
		if proxyKeys[i] == "" {
			continue
		}
		got, err := h.store.GetCredentialByProxyKey(context.Background(), proxyKeys[i])
		if err != nil {
			t.Errorf("store read #%d: %v", i, err)
			continue
		}
		rowVersionSeen[got.RowVersion]++
	}
	// Every new row is created independently and starts at row_version=1.
	if rowVersionSeen[1] != n {
		t.Errorf("expected %d rows at row_version=1, got %d (counts=%v)", n, rowVersionSeen[1], rowVersionSeen)
	}
}

// ── no If-Match contract ──────────────────────────────────────────────

// TestUpdateNoIfMatchNeeded verifies PUT succeeds without any If-Match header
// (concurrency is handled server-internally now).
func TestUpdateNoIfMatchNeeded(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	created := createCredential(t, h, "u1", "https://api.example.com", "default", "sk-old", "openai")

	rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey, map[string]interface{}{
		"api_key": "sk-new", "auth_type": "openai",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s, want 200", rec.Code, rec.Body.String())
	}
}

// TestDeleteNoIfMatchNeeded verifies DELETE succeeds without any If-Match
// header (concurrency is handled server-internally now).
func TestDeleteNoIfMatchNeeded(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	created := createCredential(t, h, "u1", "https://api.example.com", "default", "sk", "openai")

	rec := h.do(t, "DELETE", "/v1/credentials/"+created.ProxyKey, nil)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status=%d body=%s, want 204", rec.Code, rec.Body.String())
	}
}

// TestLastWriteWins verifies two consecutive PUTs both succeed and the second
// payload is the one that persists (last-write-wins).
func TestLastWriteWins(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	created := createCredential(t, h, "u1", "https://api.example.com", "default", "sk-original", "openai")

	for _, apiKey := range []string{"sk-first", "sk-second"} {
		rec := h.do(t, "PUT", "/v1/credentials/"+created.ProxyKey, map[string]interface{}{
			"api_key": apiKey, "auth_type": "openai",
		})
		if rec.Code != http.StatusOK {
			t.Fatalf("PUT %s: status=%d body=%s, want 200", apiKey, rec.Code, rec.Body.String())
		}
	}

	rec := h.do(t, "GET", "/v1/credentials/"+created.ProxyKey, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET: status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp credentialResponse
	decodeEnvelope(t, rec.Body.Bytes(), &resp)
	if resp.APIKey != "sk-second" {
		t.Errorf("api_key=%q, want sk-second (second write wins)", resp.APIKey)
	}
}

// TestResponseHasNoID verifies the create response's data object carries no
// id field (the wire structs dropped ID/RowVersion).
func TestResponseHasNoID(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var data map[string]json.RawMessage
	decodeEnvelope(t, rec.Body.Bytes(), &data)
	if _, ok := data["id"]; ok {
		t.Error("create response contains an id field; want none")
	}
}

// TestResponseHasNoRowVersion verifies the create response's data object
// carries no row_version field.
func TestResponseHasNoRowVersion(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var data map[string]json.RawMessage
	decodeEnvelope(t, rec.Body.Bytes(), &data)
	if _, ok := data["row_version"]; ok {
		t.Error("create response contains a row_version field; want none")
	}
}

// TestCreateResponseNoAPIKey verifies the create response never echoes the
// plaintext api_key.
func TestCreateResponseNoAPIKey(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-secret", "auth_type": "openai",
	})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var data map[string]json.RawMessage
	decodeEnvelope(t, rec.Body.Bytes(), &data)
	if _, ok := data["api_key"]; ok {
		t.Error("create response contains an api_key field; want none")
	}
}

// TestListResponseNoID verifies list items carry no id field.
func TestListResponseNoID(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	if r := h.do(t, "POST", "/v1/credentials", map[string]string{
		"user_id": "u1", "api_base": "https://api.example.com", "key_tag": "default",
		"api_key": "sk-abc", "auth_type": "openai",
	}); r.Code != http.StatusCreated {
		t.Fatalf("create: status=%d body=%s", r.Code, r.Body.String())
	}

	rec := h.do(t, "GET", "/v1/credentials?limit=10", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list: status=%d body=%s", rec.Code, rec.Body.String())
	}
	var list struct {
		Items []map[string]json.RawMessage `json:"items"`
	}
	decodeEnvelope(t, rec.Body.Bytes(), &list)
	if len(list.Items) != 1 {
		t.Fatalf("list items=%d, want 1", len(list.Items))
	}
	if _, ok := list.Items[0]["id"]; ok {
		t.Error("list item contains an id field; want none")
	}
}
