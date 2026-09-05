package proxy_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"credential_router/internal/credmgr/cache"
	"credential_router/internal/platform"
	"credential_router/internal/proxy"
	"credential_router/internal/credmgr"
)

// --- Mock credmgr.Getter ---

type mockVault struct {
	getFunc func(proxyKey string) (string, string, error)
	calls   atomic.Int64
}

func (m *mockVault) GetCredentialByProxyKey(proxyKey string) (string, string, error) {
	m.calls.Add(1)
	if m.getFunc != nil {
		return m.getFunc(proxyKey)
	}
	return "mock-key", "openai", nil
}

var _ cache.Getter = (*mockVault)(nil)

// --- URL helpers ---

func encodeURLBase64Safe(s string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(s))
}

const (
	testProxyKey  = "cr_pk_testproxykey00000000000000000000000000000000"
	testProxyKey2 = "cr_pk_testproxykey20000000000000000000000000000000"
	testProxyKey3 = "cr_pk_testproxykey30000000000000000000000000000000"
)

// proxyRequestPath builds the placeholder-form URL (form A):
// /proxy/{api_base_b64}/{rest}, with the proxy key carried in a header.
func proxyRequestPath(t *testing.T, apiBaseURL, path string) string {
	t.Helper()
	return "proxy/" + encodeURLBase64Safe(apiBaseURL) + "/" + path
}

func proxyRequest(t *testing.T, proxyKey, apiBaseURL, path string) *http.Request {
	t.Helper()
	req := httptest.NewRequest("GET", "/"+proxyRequestPath(t, apiBaseURL, path), nil)
	req.Header.Set("Authorization", "Bearer "+proxyKey)
	return req
}

func newHandlerForTest(cfg platform.Config, mv cache.Getter) *proxy.Handler {
	h, _ := proxy.NewHandler(cfg, mv)
	return h
}

func httpGetWithProxyKey(t *testing.T, client *http.Client, baseURL, proxyKey, apiBaseURL, path string) (*http.Response, error) {
	t.Helper()
	req, err := http.NewRequest("GET", baseURL+"/"+proxyRequestPath(t, apiBaseURL, path), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+proxyKey)
	return client.Do(req)
}

// --- Tests ---

func TestHandlerHealth(t *testing.T) {
	cfg := platform.Default()
	h, err := proxy.NewHandler(cfg, &mockVault{})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	req := httptest.NewRequest("GET", "/health", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Errorf("status=%d want 200", w.Code)
	}
	if w.Body.String() != "ok" {
		t.Errorf("body=%q want ok", w.Body.String())
	}
}

func TestHandlerMissingProxyPrefix(t *testing.T) {
	cfg := platform.Default()
	h, err := proxy.NewHandler(cfg, &mockVault{})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	req := httptest.NewRequest("GET", "/no-prefix", nil)
	req.RemoteAddr = "127.0.0.1:12345"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 404 {
		t.Errorf("status=%d want 404", w.Code)
	}
}

func TestHandlerMissingProxyKey401(t *testing.T) {
	upCalled := false
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upCalled = true
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	// Form-A path carries no proxy key and no auth header is set.
	req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 401 {
		t.Errorf("status=%d want 401", resp.StatusCode)
	}
	if upCalled {
		t.Errorf("upstream was called despite missing proxy key")
	}
}

func TestProxyKeyFromAuthorizationHeader(t *testing.T) {
	var gotProxyKey string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			gotProxyKey = proxyKey
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status=%d want 200", resp.StatusCode)
	}
	if gotProxyKey != testProxyKey {
		t.Errorf("credential lookup proxyKey=%q want %q", gotProxyKey, testProxyKey)
	}
}

func TestProxyKeyFromXApiKeyHeader(t *testing.T) {
	var gotProxyKey string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			gotProxyKey = proxyKey
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("X-Api-Key", testProxyKey)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status=%d want 200", resp.StatusCode)
	}
	if gotProxyKey != testProxyKey {
		t.Errorf("credential lookup proxyKey=%q want %q", gotProxyKey, testProxyKey)
	}
}

func TestProxyKeyFromXGoogApiKeyHeader(t *testing.T) {
	var gotProxyKey string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			gotProxyKey = proxyKey
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("X-Goog-Api-Key", testProxyKey)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status=%d want 200", resp.StatusCode)
	}
	if gotProxyKey != testProxyKey {
		t.Errorf("credential lookup proxyKey=%q want %q", gotProxyKey, testProxyKey)
	}
}

// TestProxyKeyPriority verifies the header precedence contract:
// Authorization: Bearer > X-Api-Key > X-Goog-Api-Key.
func TestProxyKeyPriority(t *testing.T) {
	tests := []struct {
		name       string
		authorization string
		xAPIKey    string
		xGoogAPIKey string
		want       string
	}{
		{"authorization wins over both", "Bearer " + testProxyKey, testProxyKey2, testProxyKey3, testProxyKey},
		{"x-api-key wins over x-goog-api-key", "", testProxyKey2, testProxyKey3, testProxyKey2},
		{"x-goog-api-key alone", "", "", testProxyKey3, testProxyKey3},
		{"authorization alone", "Bearer " + testProxyKey, "", "", testProxyKey},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var gotProxyKey string
			up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(200)
			}))
			defer up.Close()

			mv := &mockVault{
				getFunc: func(proxyKey string) (string, string, error) {
					gotProxyKey = proxyKey
					return "key-abc", "openai", nil
				},
			}
			cfg := platform.Default()
			srv := httptest.NewServer(newHandlerForTest(cfg, mv))
			defer srv.Close()

			req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
			if err != nil {
				t.Fatalf("NewRequest: %v", err)
			}
			if tt.authorization != "" {
				req.Header.Set("Authorization", tt.authorization)
			}
			if tt.xAPIKey != "" {
				req.Header.Set("X-Api-Key", tt.xAPIKey)
			}
			if tt.xGoogAPIKey != "" {
				req.Header.Set("X-Goog-Api-Key", tt.xGoogAPIKey)
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("Do: %v", err)
			}
			resp.Body.Close()

			if resp.StatusCode != 200 {
				t.Fatalf("status=%d want 200", resp.StatusCode)
			}
			if gotProxyKey != tt.want {
				t.Errorf("credential lookup proxyKey=%q want %q", gotProxyKey, tt.want)
			}
		})
	}
}

// TestStripAuthHeaders verifies the handler strips all three auth header
// families (Authorization, X-Api-Key, X-Goog-Api-Key) — including case
// variants — before injecting the real key, while preserving non-auth
// headers. The credential is registered as anthropic so the injected real
// key lands in X-Api-Key; the client's proxy-key-bearing Authorization
// header must never leak upstream.
func TestStripAuthHeaders(t *testing.T) {
	var gotAuth, gotXAPIKey, gotXGoog, gotCustom string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotXAPIKey = r.Header.Get("X-Api-Key")
		gotXGoog = r.Header.Get("X-Goog-Api-Key")
		gotCustom = r.Header.Get("X-Custom")
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "real-anthropic-key", "anthropic", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	// Proxy key rides in X-Api-Key; the other families carry client-supplied
	// values (including case variants) that must be stripped before forward.
	req.Header.Set("X-Api-Key", testProxyKey)
	req.Header.Set("Authorization", "Bearer client-fake-token")
	req.Header.Set("x-authorization", "Bearer case-variant-token")
	req.Header.Set("X-Goog-Api-Key", "client-google-fake")
	req.Header.Set("x-goog-api-key", "client-google-case")
	req.Header.Set("X-Custom", "keep-me")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status=%d want 200", resp.StatusCode)
	}
	// The injected real key replaces the client's X-Api-Key (proxy key).
	if gotXAPIKey != "real-anthropic-key" {
		t.Errorf("upstream X-Api-Key=%q want %q (client proxy key must not leak)", gotXAPIKey, "real-anthropic-key")
	}
	if gotAuth != "" {
		t.Errorf("upstream Authorization=%q want empty (stripped)", gotAuth)
	}
	if gotXGoog != "" {
		t.Errorf("upstream X-Goog-Api-Key=%q want empty (stripped)", gotXGoog)
	}
	if gotCustom != "keep-me" {
		t.Errorf("upstream X-Custom=%q want keep-me (non-auth header preserved)", gotCustom)
	}
}

func TestHandlerPropagatesProxyKeyToCredentialLookup(t *testing.T) {
	var gotProxyKeys []string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			gotProxyKeys = append(gotProxyKeys, proxyKey)
			return "key-for-" + proxyKey, "openai", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	proxyKeys := []string{testProxyKey, testProxyKey2, testProxyKey3}
	for _, pk := range proxyKeys {
		resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, pk, up.URL, "api")
		if err != nil {
			t.Fatalf("Get %s: %v", pk, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Errorf("proxy_key=%s status=%d want 200", pk, resp.StatusCode)
		}
	}

	if len(gotProxyKeys) != len(proxyKeys) {
		t.Fatalf("GetCredentialByProxyKey called %d times, want %d (got=%v)", len(gotProxyKeys), len(proxyKeys), gotProxyKeys)
	}
	for i, w := range proxyKeys {
		if gotProxyKeys[i] != w {
			t.Errorf("call[%d] proxyKey=%q want %q", i, gotProxyKeys[i], w)
		}
	}
}

// TestMultiTenantIsolation verifies that each proxy_key only resolves to its own
// credential — tenant A never gets tenant B's key and vice versa. The mock
// returns key-specific API keys, and we assert the upstream Authorization
// header matches exactly the requesting proxy_key.
func TestMultiTenantIsolation(t *testing.T) {
	type tenant struct {
		proxyKey string
		apiKey   string
	}
	tenants := []tenant{
		{testProxyKey, "alice-secret-key"},
		{testProxyKey2, "bob-secret-key"},
		{testProxyKey3, "carol-secret-key"},
	}

	var gotAuth []string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = append(gotAuth, r.Header.Get("Authorization"))
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			switch proxyKey {
			case testProxyKey:
				return "alice-secret-key", "openai", nil
			case testProxyKey2:
				return "bob-secret-key", "openai", nil
			default:
				return "carol-secret-key", "openai", nil
			}
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	for _, tnt := range tenants {
		resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, tnt.proxyKey, up.URL, "api")
		if err != nil {
			t.Fatalf("Get %s: %v", tnt.proxyKey, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Errorf("proxy_key=%s status=%d want 200", tnt.proxyKey, resp.StatusCode)
		}
	}

	if len(gotAuth) != len(tenants) {
		t.Fatalf("upstream received %d requests, want %d", len(gotAuth), len(tenants))
	}
	for i, tnt := range tenants {
		wantAuth := "Bearer " + tnt.apiKey
		if gotAuth[i] != wantAuth {
			t.Errorf("request[%d] proxy_key=%s upstream Authorization=%q want %q",
				i, tnt.proxyKey, gotAuth[i], wantAuth)
		}
		// Verify no cross-tenant leakage in the same request
		for _, other := range tenants {
			if other.proxyKey == tnt.proxyKey {
				continue
			}
			if gotAuth[i] == "Bearer "+other.apiKey {
				t.Errorf("proxy_key=%s received another tenant's key %q", tnt.proxyKey, gotAuth[i])
			}
		}
	}
}

func TestHandlerUpstreamBadGateway(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hj, ok := w.(http.Hijacker)
		if ok {
			conn, _, _ := hj.Hijack()
			conn.Close()
		}
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 500
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 502 && resp.StatusCode != 503 && resp.StatusCode != 504 {
		t.Errorf("status=%d want 502/503/504", resp.StatusCode)
	}
	if mv.calls.Load() == 0 {
		t.Errorf("credmgr.GetCredential was never called")
	}
}

func TestHandlerUpstreamTimeout(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(500 * time.Millisecond)
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 100
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := httpGetWithProxyKey(t, client, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 502 && resp.StatusCode != 504 {
		t.Errorf("status=%d want 502/504", resp.StatusCode)
	}
}

func TestHandlerStreamBody(t *testing.T) {
	body := "hello world"
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(200)
		_, _ = io.WriteString(w, body)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 2000
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	gotBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		t.Errorf("status=%d want 200, body=%s", resp.StatusCode, gotBody)
	}
	if !strings.Contains(string(gotBody), body) {
		t.Errorf("body=%q want contains %q", gotBody, body)
	}
}

func TestHandlerHopByHopStripped(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Connection", "X-Custom-Hop")
		w.Header().Set("X-Custom-Hop", "should-be-stripped")
		w.Header().Set("Content-Type", "text/plain")
		w.Header().Set("X-Keep", "should-be-kept")
		w.WriteHeader(200)
		_, _ = io.WriteString(w, "ok")
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 2000
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Errorf("status=%d want 200", resp.StatusCode)
	}
	if got := resp.Header.Get("X-Custom-Hop"); got != "" {
		t.Errorf("X-Custom-Hop=%q want empty (stripped)", got)
	}
	if got := resp.Header.Get("X-Keep"); got != "should-be-kept" {
		t.Errorf("X-Keep=%q want should-be-kept", got)
	}
}

func TestHandlerInjectKeyError(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "totally-unsupported-auth-type", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 2000
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 500 {
		t.Errorf("status=%d want 500", resp.StatusCode)
	}
}

func TestHandlerCredentialNotFound(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "", "", credmgr.ErrCredentialNotFound
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 2000
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 401 {
		t.Errorf("status=%d want 401", resp.StatusCode)
	}
}

func TestHandlerMaxBytesReader(t *testing.T) {
	cfg := platform.Default()
	cfg.Server.MaxResponseBytes = 10
	h, err := proxy.NewHandler(cfg, &mockVault{})
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	body := strings.Repeat("x", 100)
	req := httptest.NewRequest("POST", "/proxy/default/foo/api", bytes.NewReader([]byte(body)))
	req.RemoteAddr = "127.0.0.1:12345"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code < 400 || w.Code >= 600 {
		t.Errorf("status=%d want 4xx/5xx", w.Code)
	}
}

func TestHandlerRequestBodyTooLarge_PreFlight(t *testing.T) {
	upstreamCalled := atomic.Bool{}
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled.Store(true)
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	cfg.Server.MaxRequestBytes = 1024
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	oversize := bytes.Repeat([]byte("x"), 4096)
	req, err := http.NewRequest("POST",
		srv.URL+"/"+proxyRequestPath(t, up.URL, "api"),
		bytes.NewReader(oversize))
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+testProxyKey)
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Errorf("status=%d want 413 (pre-flight Content-Length rejection)", resp.StatusCode)
	}
	if upstreamCalled.Load() {
		t.Errorf("upstream was called despite 413 pre-flight — handler forwarded past body-cap check")
	}
}

func TestHandlerRequestBodyTooLarge_Chunked(t *testing.T) {
	var upstreamBytes atomic.Int64
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n, _ := io.Copy(io.Discard, r.Body)
		upstreamBytes.Store(n)
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	cfg.Server.MaxRequestBytes = 1024
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	const fullBody = 1 << 20
	body := &io.LimitedReader{R: io.LimitReader(&neverEnding{}, fullBody), N: fullBody}
	req, err := http.NewRequest("POST",
		srv.URL+"/"+proxyRequestPath(t, up.URL, "api"),
		body)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+testProxyKey)
	req.ContentLength = -1
	req.TransferEncoding = []string{"chunked"}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Errorf("status=%d want 413 (chunked body overflow)", resp.StatusCode)
	}
	got := upstreamBytes.Load()
	if got >= int64(fullBody) {
		t.Errorf("upstream received %d bytes (>= full body %d) — cap not enforced on chunked path",
			got, fullBody)
	}
}

type neverEnding struct{ off int }

func (n *neverEnding) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = 'a'
	}
	n.off += len(p)
	return len(p), nil
}

func captureTruncateLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	orig := slog.Default()
	t.Cleanup(func() { slog.SetDefault(orig) })
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	return &buf
}

func TestProxyResponseOversize(t *testing.T) {
	logBuf := captureTruncateLog(t)

	const upstreamSize = 64 * 1024
	upstreamBody := bytes.Repeat([]byte("x"), upstreamSize)

	var upstreamCancelled atomic.Bool
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", strconv.Itoa(upstreamSize))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(upstreamBody)
		<-r.Context().Done()
		upstreamCancelled.Store(true)
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	cfg.Server.MaxResponseBytes = 1024
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	got, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status=%d want 502 (clean pre-flight rejection)", resp.StatusCode)
	}
	if strings.Contains(string(got), "x") {
		t.Errorf("client body contained upstream bytes: got %d bytes %q", len(got), got)
	}
	if want := proxy.ClientMessageForTesting(platform.CodeBadGateway); !strings.Contains(string(got), want) {
		t.Errorf("client body=%q want contains %q", got, want)
	}

	deadline := time.Now().Add(2 * time.Second)
	for !upstreamCancelled.Load() && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !upstreamCancelled.Load() {
		t.Errorf("upstream request context was not cancelled within timeout")
	}

	logs := logBuf.String()
	if !strings.Contains(logs, "truncate") || !strings.Contains(logs, "content_length_exceeded") {
		t.Errorf("expected truncate/content_length_exceeded warn log, got: %s", logs)
	}
}

func TestProxyResponseOversizeChunked(t *testing.T) {
	logBuf := captureTruncateLog(t)

	const chunk = 8 * 1024
	const chunks = 256
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		flusher, _ := w.(http.Flusher)
		for i := 0; i < chunks; i++ {
			_, _ = w.Write(bytes.Repeat([]byte("x"), chunk))
			if flusher != nil {
				flusher.Flush()
			}
		}
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	cfg.Server.MaxResponseBytes = 1024
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	got, readErr := io.ReadAll(resp.Body)

	// Mid-stream the status line is already on the wire, so a clean 502 is
	// impossible (HTTP protocol limitation). We assert truncation only.
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status=%d want 200 (status line already sent for chunked)", resp.StatusCode)
	}
	maxForward := cfg.Server.MaxResponseBytes + 64*1024
	if int64(len(got)) > maxForward+int64(32*1024) {
		t.Errorf("received %d bytes, want ≤ %d (MaxResponseBytes+64KiB+buffer)",
			len(got), maxForward+int64(32*1024))
	}
	if int64(len(got)) >= int64(chunk*chunks) {
		t.Errorf("body not truncated: got %d bytes, upstream total %d", len(got), chunk*chunks)
	}
	t.Logf("ReadAll err=%v bodyLen=%d (truncation expected)", readErr, len(got))

	logs := logBuf.String()
	if !strings.Contains(logs, "truncate") || !strings.Contains(logs, "max_bytes_reader") {
		t.Errorf("expected truncate/max_bytes_reader warn log, got: %s", logs)
	}
}

func TestProxyResponseUnderLimit(t *testing.T) {
	body := "the quick brown fox jumps over the lazy dog"
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, body)
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	cfg.Server.MaxResponseBytes = 4096
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status=%d want 200", resp.StatusCode)
	}
	if string(got) != body {
		t.Errorf("body=%q want %q", got, body)
	}
}

func TestHandlerStreamFlushes(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		flusher, _ := w.(http.Flusher)
		for i := 0; i < 3; i++ {
			_, _ = w.Write(bytes.Repeat([]byte{byte('a' + i)}, 4096))
			if flusher != nil {
				flusher.Flush()
			}
		}
	}))
	defer up.Close()

	mv := &mockVault{	getFunc: func(proxyKey string) (string, string, error) {
		return "key", "openai", nil
	}}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	resp, err := httpGetWithProxyKey(t, http.DefaultClient, srv.URL, testProxyKey, up.URL, "api")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d, want 200", resp.StatusCode)
	}
	total, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("ReadAll: %v", err)
	}
	if len(total) != 3*4096 {
		t.Errorf("body length=%d, want %d", len(total), 3*4096)
	}
}

func TestHandlerContextCancel(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
		w.WriteHeader(200)
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 5000
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	req, _ := http.NewRequestWithContext(ctx, "GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	req.Header.Set("Authorization", "Bearer "+testProxyKey)
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
		t.Fatalf("Do succeeded with cancelled ctx, status=%d", resp.StatusCode)
	}
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Do error=%v want context.Canceled", err)
	}
}

func TestClientIP(t *testing.T) {
	tests := []struct {
		remoteAddr string
		want       string
	}{
		{"127.0.0.1:1234", "127.0.0.1"},
		{"192.168.1.1:5555", "192.168.1.1"},
		{"[::1]:8080", "::1"},
		{"invalid", "invalid"},
	}
	for _, tt := range tests {
		req := httptest.NewRequest("GET", "/", nil)
		req.RemoteAddr = tt.remoteAddr
		if got := proxy.ClientIPForTesting(req); got != tt.want {
			t.Errorf("clientIP(%q)=%q want %q", tt.remoteAddr, got, tt.want)
		}
	}
}

func TestIsTimeout(t *testing.T) {
	if !proxy.IsTimeoutForTesting(context.DeadlineExceeded) {
		t.Error("context.DeadlineExceeded should be timeout")
	}
	if proxy.IsTimeoutForTesting(io.EOF) {
		t.Error("io.EOF should NOT be timeout")
	}
}

func TestStripHopByHopHeaders(t *testing.T) {
	h := http.Header{}
	h.Set("Connection", "X-Custom-Hop")
	h.Set("X-Custom-Hop", "value")
	h.Set("Keep-Alive", "timeout=5")
	h.Set("X-Keep", "good")

	proxy.StripHopByHopHeadersForTesting(h)

	if h.Get("X-Custom-Hop") != "" {
		t.Errorf("X-Custom-Hop should be stripped (via Connection header)")
	}
	if h.Get("Keep-Alive") != "" {
		t.Errorf("Keep-Alive should be stripped (hop-by-hop)")
	}
	if h.Get("X-Keep") != "good" {
		t.Errorf("X-Keep should NOT be stripped, got=%q", h.Get("X-Keep"))
	}
}

func TestCloneHeader(t *testing.T) {
	src := http.Header{"X-Foo": []string{"a", "b"}}
	dst := proxy.CloneHeaderForTesting(src)
	dst.Set("X-Foo", "c")
	if src.Get("X-Foo") == "c" {
		t.Error("cloneHeader returned aliased slice")
	}
	if dst.Get("X-Foo") != "c" {
		t.Errorf("dst.X-Foo=%q want c", dst.Get("X-Foo"))
	}
}

func TestCopyHeader(t *testing.T) {
	src := http.Header{"X-Foo": []string{"a", "b"}}
	dst := http.Header{}
	proxy.CopyHeaderForTesting(dst, src)
	if dst.Get("X-Foo") != "a" {
		t.Errorf("dst.X-Foo=%q want a", dst.Get("X-Foo"))
	}
}

func TestClientMessage(t *testing.T) {
	tests := map[platform.Code]string{
		platform.CodeBadRequest:         "bad request",
		platform.CodeUnauthorized:       "unauthorized",
		platform.CodeNotFound:           "not found",
		platform.CodeBadGateway:         "bad gateway",
		platform.CodeServiceUnavailable: "service unavailable",
		platform.CodeGatewayTimeout:     "gateway timeout",
		platform.CodePayloadTooLarge:    "request body too large",
	}
	for code, want := range tests {
		if got := proxy.ClientMessageForTesting(code); got != want {
			t.Errorf("clientMessage(%s)=%q want %q", code, got, want)
		}
	}
}

func TestHandlerClientDisconnectCancelsUpstream(t *testing.T) {
	var upstreamCancelled atomic.Bool
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(200)
		_, _ = w.Write([]byte("chunk-one"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		select {
		case <-r.Context().Done():
			upstreamCancelled.Store(true)
		case <-time.After(3 * time.Second):
		}
	}))
	defer up.Close()

	mv := &mockVault{
		getFunc: func(proxyKey string) (string, string, error) {
			return "key-abc", "openai", nil
		},
	}
	cfg := platform.Default()
	srv := httptest.NewServer(newHandlerForTest(cfg, mv))
	defer srv.Close()

	client := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			DisableKeepAlives: true,
		},
	}
	req, err := http.NewRequest("GET", srv.URL+"/"+proxyRequestPath(t, up.URL, "api"), nil)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+testProxyKey)
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("client.Do: %v", err)
	}

	buf := make([]byte, 4)
	if _, err := io.ReadFull(resp.Body, buf); err != nil {
		t.Fatalf("ReadFull first chunk: %v", err)
	}
	if string(buf) != "chunk"[:4] {
		t.Errorf("first chunk=%q, want prefix 'chunk'", buf)
	}
	resp.Body.Close()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if upstreamCancelled.Load() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Errorf("upstream r.Context().Done() did not fire within 2s of client disconnect; ctx cancellation not propagated")
}

func TestIsTimeoutURLError(t *testing.T) {
	err := &url.Error{
		Op:  "Get",
		URL: "http://example.test",
		Err: context.DeadlineExceeded,
	}
	if !proxy.IsTimeoutForTesting(err) {
		t.Errorf("isTimeout(*url.Error wrapping DeadlineExceeded) = false, want true")
	}
}

func TestIsTimeoutContextCanceled(t *testing.T) {
	err := &url.Error{
		Op:  "Get",
		URL: "http://example.test",
		Err: context.Canceled,
	}
	if proxy.IsTimeoutForTesting(err) {
		t.Errorf("isTimeout(*url.Error wrapping Canceled) = true, want false (client disconnect is not a timeout)")
	}
}
