package admin

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"credential_router/internal/credmgr"
	"credential_router/internal/credmgr/cache"
	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/platform"
	"credential_router/internal/proxy"
	"credential_router/internal/proxy/ssrf"
)

// credentialRequest is the POST/PUT body. Identities are {user_id,
// api_base, key_tag}; the proxy_key is server-minted at INSERT and never
// accepted from the client. Row_version / overwrite_tombstone / sandbox_id
// were removed — concurrency is handled server-internally via the store's
// row_version optimistic lock.
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
	ProxyAddress string `json:"proxy_address,omitempty"`
}

func (s *Server) createCredential(w http.ResponseWriter, r *http.Request) {
	var req credentialRequest
	if err := decodeJSON(r, &req); err != nil {
		respondError(w, err)
		return
	}

	// Normalize first so the validation step below compares against the
	// canonical form (and dedup / policy checks see the same string the
	// router will match on).
	req.APIBase = proxy.NormalizeAPIBase(req.APIBase)

	vm := s.cfg.Validation
	if err := proxy.UserID(req.UserID, vm.UserIDMaxLen); err != nil {
		respondError(w, errValidation(err))
		return
	}
	if err := proxy.APIBaseWithPolicy(req.APIBase, vm.RealURLMaxLen, s.apiBasePolicy()); err != nil {
		respondError(w, errValidation(err))
		return
	}
	if err := proxy.KeyTag(req.KeyTag, vm.KeyTagMaxLen); err != nil {
		respondError(w, errValidation(err))
		return
	}
	if err := proxy.AuthType(req.AuthType, vm.AuthTypeMaxLen); err != nil {
		respondError(w, errValidation(err))
		return
	}
	if err := proxy.APIKey(req.APIKey, vm.APIKeyMaxLen); err != nil {
		respondError(w, errValidation(err))
		return
	}

	// Capture/Release pins the key snapshot across the encrypt →
	// INSERT critical section. Without this, a concurrent DEK rotation could
	// zero out the DEK we used to encrypt between encryptAPIKey and INSERT,
	// leaving the row permanently undecryptable. Releasing before the INSERT
	// would re-open that race; releasing only after the INSERT means the row's
	// dek_version is set from the captured snap, and Phase A can decide
	// correctly whether to reencrypt (dek_version < target) or skip (=
	// target).
	mgr := s.rotator.Manager()
	snap := mgr.Capture()
	defer mgr.Release(snap)

	encrypted, err := s.encryptAPIKey(req.APIKey, snap)
	if err != nil {
		respondError(w, err)
		return
	}

	cred := &store.Credential{
		UserID:       req.UserID,
		APIBase:      req.APIBase,
		KeyTag:       req.KeyTag,
		APIKeyCipher: encrypted,
		AuthType:     req.AuthType,
		KekVersion:   int64(snap.KekVersion),
		DekVersion:   int64(snap.DekVersion),
	}

	if err := s.store.InsertCredential(r.Context(), cred); err != nil {
		if errors.Is(err, platform.ErrConflict) {
			respondError(w, platform.Wrap(platform.CodeConflict, "CreateCredential", "credential exists", err))
			return
		}
		respondError(w, err)
		return
	}

	// Seed the proxy-key cache with the plaintext we just encrypted; the
	// facade's WriteThrough retries transient CAS failures before failing.
	if err := s.cache.WriteThrough(r.Context(), cred.ProxyKey, &cache.CachedCredential{
		ProxyKey: cred.ProxyKey,
		APIKey:   req.APIKey,
		AuthType: req.AuthType,
	}); err != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "CreateCredential", "cache write failed but DB committed", err))
		return
	}

	// InsertCredential populated ID, ProxyKey, RowVersion, timestamps in
	// place, so the response reflects post-insert state without a re-read.
	// Plaintext is NOT returned (Create never echoes api_key).
	respondJSON(w, http.StatusCreated, s.toCredentialResponse(cred, ""))
}

// credentialListItem omits api_key — list callers paginate for inventory,
// not for plaintext. Fetch single rows via GET /v1/credentials/{proxy_key}.
// proxy_key IS returned so clients can build proxy URLs from a listing.
type credentialListItem struct {
	UserID     string `json:"user_id"`
	APIBase    string `json:"api_base"`
	KeyTag     string `json:"key_tag"`
	AuthType   string `json:"auth_type"`
	KekVersion int64  `json:"kek_version"`
	DekVersion int64  `json:"dek_version"`
	CreatedAt  int64  `json:"created_at"`
	UpdatedAt  int64  `json:"updated_at"`
	ProxyKey   string `json:"proxy_key"`
}

const (
	listDefaultLimit = 50
	listMaxLimit     = 200
)

func parseListLimitOffset(r *http.Request) (int, int, error) {
	q := r.URL.Query()
	limit := listDefaultLimit
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return 0, 0, fmt.Errorf("limit must be a positive integer, got %q", v)
		}
		if n > listMaxLimit {
			n = listMaxLimit
		}
		limit = n
	}
	offset := 0
	if v := q.Get("offset"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 0 {
			return 0, 0, fmt.Errorf("offset must be a non-negative integer, got %q", v)
		}
		offset = n
	}
	return limit, offset, nil
}

func (s *Server) listCredentials(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := parseListLimitOffset(r)
	if err != nil {
		respondError(w, errValidation(err))
		return
	}
	creds, err := s.store.ListCredentials(r.Context(), limit, offset)
	if err != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "ListCredentials", "list failed", err))
		return
	}
	items := make([]credentialListItem, 0, len(creds))
	for _, c := range creds {
		items = append(items, credentialListItem{
			UserID:     c.UserID,
			APIBase:    c.APIBase,
			KeyTag:     c.KeyTag,
			AuthType:   c.AuthType,
			KekVersion: c.KekVersion,
			DekVersion: c.DekVersion,
			CreatedAt:  c.CreatedAt,
			UpdatedAt:  c.UpdatedAt,
			ProxyKey:   c.ProxyKey,
		})
	}
	respondJSON(w, http.StatusOK, map[string]interface{}{
		"items":  items,
		"limit":  limit,
		"offset": offset,
		"count":  len(items),
	})
}

func (s *Server) getCredential(w http.ResponseWriter, r *http.Request) {
	proxyKey := pathSegment(r, "proxy_key")

	cred, err := s.store.GetCredentialByProxyKey(r.Context(), proxyKey)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "GetCredential", "credential not found", err))
			return
		}
		respondError(w, err)
		return
	}

	// Single-credential GET returns the plaintext API key (unlike Create/PUT
	// responses). Resolve it through the cache facade so decrypt hits the
	// in-memory layer when warm.
	apiKey, _, err := s.cache.GetCredentialByProxyKey(proxyKey)
	if err != nil {
		if errors.Is(err, credmgr.ErrCredentialNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "GetCredential", "credential not found", err))
			return
		}
		respondError(w, err)
		return
	}

	respondJSON(w, http.StatusOK, s.toCredentialResponse(cred, apiKey))
}

func (s *Server) updateCredential(w http.ResponseWriter, r *http.Request) {
	proxyKey := pathSegment(r, "proxy_key")

	var req credentialRequest
	if err := decodeJSON(r, &req); err != nil {
		respondError(w, err)
		return
	}

	// Only api_key and auth_type are mutable via PUT; user_id / api_base /
	// key_tag are immutable identity, so the request body carries the same
	// five fields but the identity ones are ignored here.
	vm := s.cfg.Validation
	if req.APIKey == "" && req.AuthType == "" {
		respondError(w, platform.New(platform.CodeBadRequest, "UpdateCredential", "at least one of api_key or auth_type required"))
		return
	}
	if req.AuthType != "" {
		if err := proxy.AuthType(req.AuthType, vm.AuthTypeMaxLen); err != nil {
			respondError(w, errValidation(err))
			return
		}
	}
	if req.APIKey != "" {
		if err := proxy.APIKey(req.APIKey, vm.APIKeyMaxLen); err != nil {
			respondError(w, errValidation(err))
			return
		}
	}

	// See createCredential — same Capture/Release + KekVersion/
	// DekVersion pattern. Without pinning the snap, an UPDATE that encrypts
	// a new APIKey during a concurrent DEK rotation could write a row whose
	// cipher references a DEK the rotation is about to clear.
	mgr := s.rotator.Manager()
	snap := mgr.Capture()
	defer mgr.Release(snap)

	existing, err := s.store.GetCredentialByProxyKey(r.Context(), proxyKey)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "UpdateCredential", "credential not found", err))
			return
		}
		respondError(w, err)
		return
	}

	mergedCipher := existing.APIKeyCipher
	if req.APIKey != "" {
		enc, err := s.encryptAPIKey(req.APIKey, snap)
		if err != nil {
			respondError(w, err)
			return
		}
		mergedCipher = enc
	}
	mergedAuthType := existing.AuthType
	if req.AuthType != "" {
		mergedAuthType = req.AuthType
	}

	// Mutate `existing` in place: store.UpdateCredential increments RowVersion
	// and refreshes UpdatedAt on the struct it receives, so the response can
	// reflect the post-update state without a re-read. existing.RowVersion
	// comes from the SELECT above and drives the store's optimistic lock.
	existing.APIKeyCipher = mergedCipher
	existing.AuthType = mergedAuthType
	existing.KekVersion = int64(snap.KekVersion)
	existing.DekVersion = int64(snap.DekVersion)

	// row_version optimistic concurrency: a Phase A batch UPDATE or another
	// admin request may have advanced the row between our SELECT and the
	// UPDATE. Retry up to 3 times (re-SELECT inside retry) before failing.
	// A tiny jittered backoff between attempts avoids the livelock that
	// happens when N goroutines all retry against the same row at once.
	const maxUpdateAttempts = 3
	var lastErr error
	for attempt := 1; attempt <= maxUpdateAttempts; attempt++ {
		lastErr = s.store.UpdateCredential(r.Context(), existing.ID, existing)
		if lastErr == nil {
			break
		}
		if !errors.Is(lastErr, platform.ErrConflict) {
			break
		}
		if attempt == maxUpdateAttempts {
			break
		}
		jitteredBackoff(r.Context(), attempt)
		fresh, ferr := s.store.GetCredentialByProxyKey(r.Context(), proxyKey)
		if ferr != nil {
			lastErr = ferr
			break
		}
		existing = fresh
		existing.APIKeyCipher = mergedCipher
		existing.AuthType = mergedAuthType
		existing.KekVersion = int64(snap.KekVersion)
		existing.DekVersion = int64(snap.DekVersion)
	}
	if lastErr != nil {
		if errors.Is(lastErr, platform.ErrConflict) {
			respondError(w, platform.Wrap(platform.CodeConflict, "UpdateCredential", "row_version mismatch", lastErr))
			return
		}
		if errors.Is(lastErr, platform.ErrNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "UpdateCredential", "credential not found", lastErr))
			return
		}
		respondError(w, lastErr)
		return
	}

	if err := s.writeThroughPartial(r.Context(), existing.ProxyKey, req.APIKey, mergedAuthType); err != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "UpdateCredential", "cache write failed but DB committed", err))
		return
	}

	// No plaintext echo on PUT either.
	respondJSON(w, http.StatusOK, s.toCredentialResponse(existing, ""))
}

// writeThroughPartial refreshes the cache after a partial update. When newAPIKey
// is non-empty the caller provided the new plaintext directly; otherwise the
// existing plaintext must come from the cache (a miss means the next GET will
// repopulate from DB, so we skip WriteThrough rather than re-decrypt).
func (s *Server) writeThroughPartial(ctx context.Context, proxyKey, newAPIKey, mergedAuthType string) error {
	plaintext := newAPIKey
	if plaintext == "" {
		cached, err := s.cache.Peek(proxyKey)
		if err != nil || cached == nil {
			return nil // best-effort: next GET will refresh from DB
		}
		plaintext = cached.APIKey
	}
	return s.cache.WriteThrough(ctx, proxyKey, &cache.CachedCredential{
		ProxyKey: proxyKey,
		APIKey:   plaintext,
		AuthType: mergedAuthType,
	})
}

func (s *Server) deleteCredential(w http.ResponseWriter, r *http.Request) {
	proxyKey := pathSegment(r, "proxy_key")

	cred, err := s.store.GetCredentialByProxyKey(r.Context(), proxyKey)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "DeleteCredential", "credential not found", err))
			return
		}
		respondError(w, err)
		return
	}

	const maxDeleteAttempts = 3
	var delErr error
	for attempt := 1; attempt <= maxDeleteAttempts; attempt++ {
		delErr = s.store.DeleteCredential(r.Context(), cred.ID, cred.RowVersion)
		if delErr == nil {
			break
		}
		if !errors.Is(delErr, platform.ErrConflict) {
			break
		}
		if attempt == maxDeleteAttempts {
			break
		}
		fresh, ferr := s.store.GetCredentialByProxyKey(r.Context(), proxyKey)
		if ferr != nil {
			delErr = ferr
			break
		}
		cred = fresh
	}
	if delErr != nil {
		if errors.Is(delErr, platform.ErrConflict) {
			respondError(w, platform.Wrap(platform.CodeConflict, "DeleteCredential", "row_version mismatch", delErr))
			return
		}
		respondError(w, delErr)
		return
	}

	// Accepted race (D): a reader that already passed cache.Get with a miss
	// and observed the row before this DELETE can Put stale plaintext after
	// the tombstone lands; putLocked does not distinguish Entry kinds. The
	// window is bounded by reader DB-read → cache-Put latency (typical
	// <1ms) and is only reachable during concurrent admin DELETE.
	s.cache.PutTombstone(proxyKey)

	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) encryptAPIKey(apiKey string, snap *keystore.KeySnapshot) ([]byte, error) {
	blob, err := crypto.EncryptCredential(snap.CryptoMode, snap.DEK.Bytes(), []byte(apiKey))
	if err != nil {
		return nil, platform.Wrap(platform.CodeInternal, "EncryptAPIKey", "encryption failed", err)
	}
	return blob, nil
}

func (s *Server) apiBasePolicy() *ssrf.URLPolicy {
	if s.policyFactory != nil {
		return s.policyFactory()
	}
	p := ssrf.DefaultPolicy()
	if hosts := s.ssrf.AllowedHosts; len(hosts) > 0 {
		p.AllowedHosts = hosts
	}
	return p
}

// proxyAddress is the base URL a client prefixes with "/<rest>" to reach the
// credential through the proxy. It depends on the proxy bind address (known
// only to the admin layer), so the proxy handler never reconstructs it.
// When bind is wildcard (0.0.0.0/::), the wildcard is not a valid client target
// — use server.external_address instead. Validate() rejects wildcard+empty
// external at startup; the empty return is defensive for direct callers.
func (s *Server) proxyAddress(apiBase string) string {
	host, _, err := net.SplitHostPort(s.serverCfg.BindAddress)
	if err != nil {
		host = s.serverCfg.BindAddress
	}
	if host == "0.0.0.0" || host == "::" || host == "" {
		if s.serverCfg.ExternalAddress != "" {
			return strings.TrimRight(s.serverCfg.ExternalAddress, "/") + "/proxy/" + proxy.EncodeBase64URL(apiBase)
		}
		return ""
	}
	return fmt.Sprintf("http://%s/proxy/%s", s.serverCfg.BindAddress, proxy.EncodeBase64URL(apiBase))
}

// jitteredBackoff sleeps for a small, attempt-scaled, randomised window
// so concurrent admin retries don't synchronise into the same row and
// livelock. It respects ctx cancellation. Tuned for in-process admin
// contention (millisecond scale) — not a substitute for upstream rate
// limits on the client side.
func jitteredBackoff(ctx context.Context, attempt int) {
	base := time.Duration(attempt) * time.Millisecond
	jitter := time.Duration(rand.Int63n(int64(2 * time.Millisecond)))
	d := base + jitter
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
	case <-t.C:
	}
}

func (s *Server) toCredentialResponse(c *store.Credential, apiKey string) credentialResponse {
	return credentialResponse{
		UserID:       c.UserID,
		APIBase:      c.APIBase,
		KeyTag:       c.KeyTag,
		APIKey:       apiKey,
		AuthType:     c.AuthType,
		KekVersion:   c.KekVersion,
		DekVersion:   c.DekVersion,
		CreatedAt:    c.CreatedAt,
		UpdatedAt:    c.UpdatedAt,
		ProxyKey:     c.ProxyKey,
		ProxyAddress: s.proxyAddress(c.APIBase),
	}
}
