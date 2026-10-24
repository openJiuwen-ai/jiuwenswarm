package tests_test

import (
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

var realHTTPClient = &http.Client{Timeout: 30 * time.Second}

func TestE2E_Real_OpenAI(t *testing.T) {
	cred := loadRealCred(t, openAICredSpec)

	router := startRouter(t, routerConfig{Credentials: []credentialEntry{cred}})

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, cred.APIBase, "/models"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+proxyKeyFor(t, router, "", cred.APIBase, cred.KeyTag))

	resp, err := realHTTPClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d body = %s", resp.StatusCode, truncateBody(body))
	}
	if len(body) == 0 {
		t.Fatal("empty response body")
	}
}

func TestE2E_Real_Anthropic(t *testing.T) {
	cred := loadRealCred(t, anthropicCredSpec)

	router := startRouter(t, routerConfig{Credentials: []credentialEntry{cred}})

	model := strings.TrimSpace(os.Getenv("E2E_ANTHROPIC_MODEL"))
	if model == "" {
		model = "claude-3-5-haiku-latest"
	}
	payload := `{"model":"` + model + `","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}`
	req, err := http.NewRequest(http.MethodPost, proxyURL(router.BaseURL, cred.APIBase, "/messages"), strings.NewReader(payload))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+proxyKeyFor(t, router, "", cred.APIBase, cred.KeyTag))
	req.Header.Set("X-Api-Key", "fake-placeholder")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("anthropic-version", "2023-06-01")

	resp, err := realHTTPClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d body = %s", resp.StatusCode, truncateBody(body))
	}
	if len(body) == 0 {
		t.Fatal("empty response body")
	}
}

func TestE2E_Real_Google(t *testing.T) {
	cred := loadRealCred(t, googleCredSpec)

	router := startRouter(t, routerConfig{Credentials: []credentialEntry{cred}})

	req, err := http.NewRequest(http.MethodGet, proxyURL(router.BaseURL, cred.APIBase, "/models"), nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+proxyKeyFor(t, router, "", cred.APIBase, cred.KeyTag))
	req.Header.Set("X-Goog-Api-Key", "***")

	resp, err := realHTTPClient.Do(req)
	if err != nil {
		t.Fatalf("do request: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d body = %s", resp.StatusCode, truncateBody(body))
	}
	if len(body) == 0 {
		t.Fatal("empty response body")
	}
}

func truncateBody(body []byte) string {
	const limit = 512
	if len(body) <= limit {
		return string(body)
	}
	return string(body[:limit]) + "..."
}
