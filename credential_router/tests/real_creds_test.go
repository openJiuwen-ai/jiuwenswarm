package tests_test

import (
	"os"
	"strings"
	"testing"
)

type realCredSpec struct {
	EnvKey     string
	EnvURL     string
	EnvKeyTag  string
	AuthType   string
	DefaultURL string
}

var (
	openAICredSpec = realCredSpec{
		EnvKey:     "E2E_OPENAI_API_KEY",
		EnvURL:     "E2E_OPENAI_REAL_URL",
		EnvKeyTag:  "E2E_OPENAI_KEY_TAG",
		AuthType:   "openai",
		DefaultURL: "https://api.openai.com/v1",
	}
	anthropicCredSpec = realCredSpec{
		EnvKey:     "E2E_ANTHROPIC_API_KEY",
		EnvURL:     "E2E_ANTHROPIC_REAL_URL",
		EnvKeyTag:  "E2E_ANTHROPIC_KEY_TAG",
		AuthType:   "anthropic",
		DefaultURL: "https://api.anthropic.com/v1",
	}
	googleCredSpec = realCredSpec{
		EnvKey:     "E2E_GOOGLE_API_KEY",
		EnvURL:     "E2E_GOOGLE_REAL_URL",
		EnvKeyTag:  "E2E_GOOGLE_KEY_TAG",
		AuthType:   "google",
		DefaultURL: "https://generativelanguage.googleapis.com/v1beta",
	}
)

func loadRealCred(t *testing.T, spec realCredSpec) credentialEntry {
	t.Helper()

	apiKey := strings.TrimSpace(os.Getenv(spec.EnvKey))
	if apiKey == "" {
		t.Skipf("%s not set, skipping real API test", spec.EnvKey)
	}

	realURL := strings.TrimSpace(os.Getenv(spec.EnvURL))
	if realURL == "" {
		realURL = spec.DefaultURL
	}
	realURL = strings.TrimSuffix(realURL, "/")

	keyTag := strings.TrimSpace(os.Getenv(spec.EnvKeyTag))
	if keyTag == "" {
		keyTag = "default"
	}

	return credentialEntry{
		APIBase: realURL,
		KeyTag:   keyTag,
		APIKey:   apiKey,
		AuthType: spec.AuthType,
	}
}
