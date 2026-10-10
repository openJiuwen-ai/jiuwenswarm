package admin

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"credential_router/internal/credmgr/cache"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/platform"
	"credential_router/internal/proxy/ssrf"
)

type Server struct {
	cfg         platform.AdminConfig
	ssrf        platform.SSRFConfig
	store       *store.Store
	cache       *cache.CachedCredentialGetter
	rotator     *keystore.Rotator
	serverCfg   platform.ServerConfig
	rotationCfg platform.RotationConfig
	recoveryCfg platform.RecoveryConfig

	// policyFactory is the hook realURLPolicy uses to build the SSRF
	// URLPolicy. Defaults to a DefaultPolicy + AllowedHosts override.
	// Tests in this package swap it to TestPolicy so credential fixtures
	// (e.g. api.example.com) do not hit DNS.
	policyFactory func() *ssrf.URLPolicy
}

func NewServer(cfg platform.AdminConfig, ssrfCfg platform.SSRFConfig, serverCfg platform.ServerConfig, rotationCfg platform.RotationConfig, recoveryCfg platform.RecoveryConfig, s *store.Store, ccg *cache.CachedCredentialGetter, rot *keystore.Rotator) *Server {
	return &Server{
		cfg:         cfg,
		ssrf:        ssrfCfg,
		store:       s,
		cache:       ccg,
		rotator:     rot,
		serverCfg:   serverCfg,
		rotationCfg: rotationCfg,
		recoveryCfg: recoveryCfg,
	}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", s.health)
	mux.HandleFunc("GET /v1/keystore/status", s.keystoreStatus)
	mux.HandleFunc("POST /v1/keystore/shards", s.shardRotate)
	mux.HandleFunc("POST /v1/keystore/rotate-dek", s.dekRotate)
	mux.HandleFunc("POST /v1/credentials", s.createCredential)
	mux.HandleFunc("GET /v1/credentials", s.listCredentials)
	mux.HandleFunc("GET /v1/credentials/{proxy_key}", s.getCredential)
	mux.HandleFunc("PUT /v1/credentials/{proxy_key}", s.updateCredential)
	mux.HandleFunc("DELETE /v1/credentials/{proxy_key}", s.deleteCredential)
	return s.withBodyLimit(mux)
}

func (s *Server) withBodyLimit(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ContentLength > s.serverCfg.MaxRequestBytes {
			respondError(w, platform.Wrap(platform.CodePayloadTooLarge, "BodyLimit", "request body too large", nil))
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, s.serverCfg.MaxRequestBytes)
		next.ServeHTTP(w, r)
	})
}

type successEnvelope struct {
	Status string      `json:"status"`
	Data   interface{} `json:"data"`
}

type errorEnvelope struct {
	Status string    `json:"status"`
	Error  errorBody `json:"error"`
}

type errorBody struct {
	Code    platform.Code `json:"code"`
	Message string        `json:"message"`
	Op      string        `json:"op,omitempty"`
}

func respondJSON(w http.ResponseWriter, status int, body interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(successEnvelope{Status: "ok", Data: body}); err != nil {
		slog.Error("encode response", "error", err)
	}
}

func respondError(w http.ResponseWriter, err error) {
	if err == nil {
		return
	}
	code := platform.CodeOf(err)
	if code == "" {
		code = platform.CodeInternal
	}
	msg := err.Error()
	var e *platform.Error
	if errors.As(err, &e) {
		if e.Op != "" {
			slog.Error("request failed", "op", e.Op, "code", e.Code, "error", e.Error())
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if code == platform.CodeServiceUnavailable {
		w.Header().Set("Retry-After", "1")
	}
	w.WriteHeader(code.HTTPStatus())
	if err := json.NewEncoder(w).Encode(errorEnvelope{Status: "error", Error: errorBody{
		Code:    code,
		Message: msg,
	}}); err != nil {
		slog.Error("encode error response", "error", err)
	}
}

func decodeJSON(r *http.Request, v interface{}) error {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return platform.Wrap(platform.CodeBadRequest, "DecodeJSON", "invalid JSON body", err)
	}
	return nil
}

func pathSegment(r *http.Request, name string) string {
	v := r.PathValue(name)
	v = strings.TrimPrefix(v, "/")
	v = strings.TrimSuffix(v, "/")
	return v
}

func errInvalidField(field string) error {
	return platform.New(platform.CodeBadRequest, "Validate", fmt.Sprintf("invalid field: %s", field))
}

func errValidation(verr error) error {
	return platform.Wrap(platform.CodeBadRequest, "Validate", verr.Error(), verr)
}
