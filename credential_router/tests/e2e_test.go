package tests_test

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

type capturedRequest struct {
	method        string
	path          string
	rawQuery      string
	authHeader    string
	xApiKeyHeader string
	googleHeader  string
	host          string
}

func startUpstream(t *testing.T, capture *atomic.Pointer[capturedRequest]) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		capture.Store(&capturedRequest{
			method:        r.Method,
			path:          r.URL.Path,
			rawQuery:      r.URL.RawQuery,
			authHeader:    r.Header.Get("Authorization"),
			xApiKeyHeader: r.Header.Get("X-Api-Key"),
			googleHeader:  r.Header.Get("X-Goog-Api-Key"),
			host:          r.Host,
		})
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
}

func TestE2E_OpenAIForward(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "prod", APIKey: "sk-real-openai", AuthType: "openai"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "prod")

	req, err := http.NewRequest(http.MethodPost, proxyURL(router.BaseURL, upstream.URL, "/chat/completions"), strings.NewReader(`{"model":"gpt-4"}`))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d body = %s", resp.StatusCode, body)
	}
	if string(body) != `{"ok":true}` {
		t.Fatalf("body = %s", body)
	}

	got := captured.Load()
	if got == nil {
		t.Fatal("upstream did not receive request")
	}
	if got.authHeader != "Bearer sk-real-openai" {
		t.Fatalf("Authorization = %q", got.authHeader)
	}
	if got.path != "/chat/completions" {
		t.Fatalf("path = %q", got.path)
	}
}

func TestE2E_AnyFakeKeyOverwritten(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "prod", APIKey: "sk-real-openai", AuthType: "openai"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "prod")

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, upstream.URL, "/models"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.authHeader != "Bearer sk-real-openai" {
		t.Fatalf("Authorization = %q", got.authHeader)
	}
}

func TestE2E_QueryStringForwarded(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "default", APIKey: "sk-real", AuthType: "openai"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "default")

	url := proxyURL(router.BaseURL, upstream.URL, "/models") + "?limit=10&foo=bar"
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.rawQuery != "limit=10&foo=bar" {
		t.Fatalf("rawQuery = %q", got.rawQuery)
	}
}

func TestE2E_IgnoresXForwardedFor(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "default", APIKey: "sk-real", AuthType: "openai"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "default")

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, upstream.URL, "/models"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)
	req.Header.Set("X-Forwarded-For", "10.0.0.99")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()
	// Still succeeds using TCP RemoteAddr identity (stub returns fixed user), not XFF.
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestE2E_Health(t *testing.T) {
	router := startRouter(t, routerConfig{})
	resp, err := http.Get(router.BaseURL + "/health")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestE2E_AnthropicForward(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "default", APIKey: "sk-ant-real", AuthType: "anthropic"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "default")

	req, err := http.NewRequest(http.MethodPost, proxyURL(router.BaseURL, upstream.URL, "/v1/messages"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)
	req.Header.Set("X-Api-Key", "any-fake-value")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.xApiKeyHeader != "sk-ant-real" {
		t.Fatalf("X-Api-Key = %q", got.xApiKeyHeader)
	}
}

func TestE2E_GoogleForward(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "default", APIKey: "google-key", AuthType: "google"},
		},
	})

	pk := proxyKeyFor(t, router, "", upstream.URL, "default")

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, upstream.URL, "/v1/models"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+pk)
	req.Header.Set("X-Goog-Api-Key", "fake")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.googleHeader != "google-key" {
		t.Fatalf("X-Goog-Api-Key = %q", got.googleHeader)
	}
}

// TestE2E_InvalidAPIBaseSegment: the api_base_b64 first segment decodes fine
// but does not yield an http(s) URL, so the proxy rejects with 400.
func TestE2E_InvalidAPIBaseSegment(t *testing.T) {
	router := startRouter(t, routerConfig{})

	resp, err := http.Get(router.BaseURL + "/proxy/" + encodeBase64URL("not-a-url") + "/x")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestE2E_DecodeFailure(t *testing.T) {
	router := startRouter(t, routerConfig{})

	resp, err := http.Get(router.BaseURL + "/proxy/default/not-valid!!!/x")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestE2E_MissingProxyPrefix(t *testing.T) {
	router := startRouter(t, routerConfig{})

	resp, err := http.Get(router.BaseURL + "/not-proxy")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

// TestE2E_CredentialNotFound: a well-formed proxy_key that no credential
// matches resolves to 401 from the proxy (and the api_base is never reached).
func TestE2E_CredentialNotFound(t *testing.T) {
	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{APIBase: "http://other.com", KeyTag: "default", APIKey: "k", AuthType: "openai"},
		},
	})

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, "http://example.com", "/x"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+credentialRouterTestNonexistentProxyKey)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestE2E_CustomUserForward(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		StubUserID: "user_b",
		Credentials: []credentialEntry{
			{APIBase: upstream.URL, KeyTag: "dev", APIKey: "sk-dev", AuthType: "openai"},
		},
	})

	pk := proxyKeyFor(t, router, "user_b", upstream.URL, "dev")

	req, _ := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, upstream.URL, "/ping"), nil)
	req.Header.Set("Authorization", "Bearer "+pk)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.authHeader != "Bearer sk-dev" {
		t.Fatalf("Authorization = %q", got.authHeader)
	}
}

func TestE2E_UserIsolation(t *testing.T) {
	var captured atomic.Pointer[capturedRequest]
	upstream := startUpstream(t, &captured)
	defer upstream.Close()

	router := startRouter(t, routerConfig{
		Credentials: []credentialEntry{
			{UserID: "user_a", APIBase: upstream.URL, KeyTag: "prod", APIKey: "key-a", AuthType: "openai"},
			{UserID: "user_b", APIBase: upstream.URL, KeyTag: "prod", APIKey: "key-b", AuthType: "openai"},
		},
	})

	// Same api_base + key_tag under different users yield distinct proxy_keys:
	// the presented proxy_key selects exactly which api_key is injected.
	pkA := proxyKeyFor(t, router, "user_a", upstream.URL, "prod")
	pkB := proxyKeyFor(t, router, "user_b", upstream.URL, "prod")
	if pkA == pkB {
		t.Fatalf("expected distinct proxy_keys, got %q for both users", pkA)
	}

	req, _ := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, upstream.URL, "/ping"), nil)
	req.Header.Set("Authorization", "Bearer "+pkA)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	got := captured.Load()
	if got.authHeader != "Bearer key-a" {
		t.Fatalf("Authorization = %q, want key-a for user_a", got.authHeader)
	}
}
