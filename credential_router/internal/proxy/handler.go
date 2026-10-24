package proxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"sync"
	"time"

	"credential_router/internal/platform"
	"credential_router/internal/credmgr"
	"credential_router/internal/proxy/ssrf"
)

// CredentialGetter resolves a proxy_key to the plaintext API key it stands
// for. *credmgr.CredMgr and *cache.CachedCredentialGetter both implement it.
type CredentialGetter interface {
	GetCredentialByProxyKey(proxyKey string) (apiKey, authType string, err error)
}

type Handler struct {
	cfg        platform.Config
	credMgr    CredentialGetter
	httpClient *http.Client
	// parsedAPI caches url.Parse(apiBase) per proxyKey. Not invalidated
	// on PUT/DELETE — safe because PUT does not change api_base
	// (UpdateCredential SQL only touches api_key_cipher/auth_type/
	// kek_version/dek_version) and DELETE causes a cache tombstone
	// (handler returns 401 before reaching this cache). proxy_key is
	// random (cr_pk_ + 43 chars) so reuse of a deleted key is impossible.
	parsedAPI sync.Map
	// bufPool recycles the 32 KiB scratch buffer used by the response
	// body copy loop — without pooling this is ~448 MB/s of allocation
	// at 14K RPS and shows up as the dominant makeslice hotspot.
	bufPool sync.Pool
}

func NewHandler(cfg platform.Config, credMgr CredentialGetter) (*Handler, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 20
	transport.IdleConnTimeout = cfg.Server.IdleConnTimeout

	if cfg.SSRF.DialCheck {
		guard := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(),
			cfg.SSRF.CacheTTL, cfg.SSRF.Timeout)
		transport.DialContext = guard.DialContext
	}

	return &Handler{
		cfg:     cfg,
		credMgr: credMgr,
		httpClient: &http.Client{
			Timeout:   cfg.UpstreamTimeout(),
			Transport: transport,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		bufPool: sync.Pool{
			New: func() interface{} {
				b := make([]byte, 32*1024)
				return &b
			},
		},
	}, nil
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/health" {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok")
		return
	}

	// Cap inbound request body to prevent slow-loris / memory-pressure
	// DoS. The check is split into two parts:
	//   1. Content-Length pre-flight: if the client declared a length above
	//      the limit, reject before reading any body bytes — this is the
	//      cheap, deterministic path and avoids a wasteful upstream round trip.
	//   2. MaxBytesReader on r.Body: protects the chunked / unknown-length
	//      path. The wrapper is shared with the cloned outReq.Body below
	//      (r.Clone is a shallow copy of Body), so the cap is enforced when
	//      http.Client.Do forwards the body upstream. Over-limit reads fail
	//      with *http.MaxBytesError, which we map to 413 in the do() error
	//      path.
	if maxReq := h.cfg.Server.MaxRequestBytes; maxReq > 0 {
		if r.ContentLength > maxReq {
			code := platform.CodePayloadTooLarge
			h.logReject(clientIP(r), "",
				fmt.Sprintf("request body declared Content-Length %d exceeds limit %d",
					r.ContentLength, maxReq),
				code)
			http.Error(w, clientMessage(code), code.HTTPStatus())
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxReq)
	}

	sourceIP := clientIP(r)

	apiBase, originalPath, err := ParseProxyPath(r.URL.Path)
	if err != nil {
		code := platform.CodeBadRequest
		if errors.Is(err, ErrMissingProxyPrefix) {
			code = platform.CodeNotFound
		}
		h.logReject(sourceIP, "", err.Error(), code)
		http.Error(w, clientMessage(code), code.HTTPStatus())
		return
	}

	// The proxy_key comes from one of three standard LLM auth headers
	// (Authorization Bearer, X-Api-Key, X-Goog-Api-Key), in priority order.
	// proxyKeyFromHeaders returns the first non-empty trimmed token.
	proxyKey := proxyKeyFromHeaders(r)
	if proxyKey == "" {
		code := platform.CodeUnauthorized
		h.logReject(sourceIP, apiBase, "proxy_key required", code)
		http.Error(w, clientMessage(code), code.HTTPStatus())
		return
	}

	apiKey, authType, err := h.credMgr.GetCredentialByProxyKey(proxyKey)
	if err != nil {
		code := platform.CodeUnauthorized
		if !errors.Is(err, credmgr.ErrCredentialNotFound) {
			code = platform.CodeServiceUnavailable
		}
		h.logReject(sourceIP, apiBase, err.Error(), code)
		http.Error(w, clientMessage(code), code.HTTPStatus())
		return
	}

	var baseURL *url.URL
	if cached, ok := h.parsedAPI.Load(proxyKey); ok {
		baseURL = cached.(*url.URL)
	} else {
		u, perr := url.Parse(apiBase)
		if perr != nil {
			code := platform.CodeBadRequest
			h.logReject(sourceIP, apiBase, perr.Error(), code)
			http.Error(w, clientMessage(code), code.HTTPStatus())
			return
		}
		actual, _ := h.parsedAPI.LoadOrStore(proxyKey, u)
		baseURL = actual.(*url.URL)
	}
	extra := ""
	if originalPath != "" && originalPath != "/" {
		// path.Clean collapses runs of "/" (so "//v1" or "/v1//chat"
		// become "/v1/chat") and resolves "." / ".." segments, which
		// would otherwise produce double-slash or traversal-looking
		// upstream URLs (most servers normalize but some 404). Empty
		// result or "/" means the caller asked for the upstream base
		// itself, so we leave extra empty.
		//
		// Trailing "/" is stripped — OpenAI and Anthropic SDKs never
		// emit one. A curl-style caller that relies on a trailing
		// slash for routing is treated as equivalent to the trimmed
		// form; documented limitation.
		cleaned := path.Clean(originalPath)
		if cleaned != "/" {
			extra = cleaned
		}
	}
	targetURL := &url.URL{
		Scheme:   baseURL.Scheme,
		Host:     baseURL.Host,
		Path:     baseURL.Path + extra,
		RawQuery: r.URL.RawQuery,
	}

	outReq := r.Clone(r.Context())
	outReq.URL = targetURL
	outReq.Host = targetURL.Host
	outReq.RequestURI = ""

	// outReq.Header is already a deep clone from r.Clone; modify in place.
	stripHopByHopHeaders(outReq.Header)
	outReq.Header.Del("Host")
	stripAuthHeaders(outReq.Header)
	if err := InjectRealKey(outReq.Header, apiKey, authType); err != nil {
		code := platform.CodeInternal
		h.logReject(sourceIP, apiBase, err.Error(), code)
		http.Error(w, clientMessage(code), code.HTTPStatus())
		return
	}

	start := time.Now()
	resp, err := h.httpClient.Do(outReq)
	if err != nil {
		code := platform.CodeBadGateway
		if isTimeout(err) {
			code = platform.CodeGatewayTimeout
		}
		if isRequestBodyTooLarge(err) {
			code = platform.CodePayloadTooLarge
		}
		h.logReject(sourceIP, apiBase, err.Error(), code)
		http.Error(w, clientMessage(code), code.HTTPStatus())
		return
	}
	defer resp.Body.Close()

	// body.Close on ctx cancel unblocks the in-flight body.Read —
	// otherwise the LLM upstream keeps generating for a dead client.
	stopCancelWatcher := context.AfterFunc(r.Context(), func() {
		resp.Body.Close()
	})
	defer stopCancelWatcher()

	proxyKeyID := redactProxyKey(proxyKey)

	// Pre-flight: if the upstream advertises a Content-Length that
	// already exceeds MaxResponseBytes, reject before writing the status
	// line so the client gets a clean 502 instead of a half-streamed body.
	if resp.ContentLength > 0 && resp.ContentLength > h.cfg.Server.MaxResponseBytes {
		resp.Body.Close()
		slog.Warn("truncate",
			"action", "truncate",
			"api_base", apiBase,
			"proxy_key_id", proxyKeyID,
			"reason", "content_length_exceeded",
			"max_bytes", h.cfg.Server.MaxResponseBytes,
		)
		http.Error(w, clientMessage(platform.CodeBadGateway), platform.CodeBadGateway.HTTPStatus())
		return
	}

	copyHeader(w.Header(), resp.Header)
	stripHopByHopHeaders(w.Header())
	w.WriteHeader(resp.StatusCode)

	// Backpressure: wrap the upstream body so reads are capped at
	// MaxResponseBytes + 64KiB. The +64KiB margin absorbs the boundary read
	// of the 32KiB loop so a response at exactly the limit is not falsely
	// rejected.
	//
	// Once WriteHeader has been called the HTTP/1.1 status line is already
	// on the wire, so a mid-stream overflow can no longer be reported to the
	// client as a clean 502 (HTTP protocol limitation, not a bug). We close
	// the upstream body — which cancels the upstream read — and stop
	// forwarding; the client sees a truncated connection. Do NOT "fix" this
	// by buffering the whole upstream response: that breaks SSE/chunked
	// streaming (the proxy is expected to forward stream chunks as they
	// arrive, not aggregate them before flushing).
	const (
		flushEvery = 32 * 1024
		// maxBytesMargin absorbs the boundary read of the flushEvery loop so
		// a response at exactly MaxResponseBytes is not falsely truncated,
		// plus one extra chunk for clean truncation detection. Tied to
		// flushEvery: change one, change both.
		maxBytesMargin = 2 * flushEvery
	)
	maxBytes := h.cfg.Server.MaxResponseBytes + maxBytesMargin
	body := http.MaxBytesReader(w, resp.Body, maxBytes)
	bufPtr := h.bufPool.Get().(*[]byte)
	defer h.bufPool.Put(bufPtr)
	buf := *bufPtr
	for {
		select {
		case <-r.Context().Done():
			h.logReject(sourceIP, apiBase,
				"client canceled", platform.CodeBadGateway)
			return
		default:
		}
		n, rerr := body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
		if rerr != nil {
			if rerr == io.EOF || errors.Is(rerr, io.ErrUnexpectedEOF) {
				break
			}
			var maxErr *http.MaxBytesError
			if errors.As(rerr, &maxErr) {
				resp.Body.Close()
				slog.Warn("truncate",
					"action", "truncate",
					"api_base", apiBase,
					"proxy_key_id", proxyKeyID,
					"reason", "max_bytes_reader",
					"max_bytes", h.cfg.Server.MaxResponseBytes,
				)
				return
			}
			// AfterFunc may have closed resp.Body on client cancel, making
			// the read error look generic. Re-check ctx to attribute.
			if r.Context().Err() != nil {
				h.logReject(sourceIP, apiBase,
					"client canceled", platform.CodeBadGateway)
				return
			}
			h.logReject(sourceIP, apiBase,
				"upstream read failed: "+rerr.Error(), platform.CodeBadGateway)
			return
		}
	}

	slog.Info("forward",
		"action", "forward",
		"source_ip", sourceIP,
		"proxy_key_id", proxyKeyID,
		"api_base", apiBase,
		"status", resp.StatusCode,
		"latency_ms", time.Since(start).Milliseconds(),
	)
}

// proxyKeyFromHeaders extracts the proxy_key from the request headers,
// iterating proxyKeySourceHeaders (defined in auth_headers.go) in priority
// order. Returns the first non-empty token.
//
// Authorization is special: it requires a "Bearer <token>" form (scheme
// name matched case-insensitively per RFC 7235 §2.1, single token, no
// whitespace). Other headers are accepted verbatim (trimmed).
func proxyKeyFromHeaders(r *http.Request) string {
	const bearerPrefix = "Bearer "
	for _, name := range proxyKeySourceHeaders {
		v := r.Header.Get(name)
		if v == "" {
			continue
		}
		if name == HeaderAuthorization {
			if len(v) <= len(bearerPrefix) || !strings.EqualFold(v[:len(bearerPrefix)], bearerPrefix) {
				continue
			}
			rest := v[len(bearerPrefix):]
			if rest == "" || rest != strings.TrimSpace(rest) || strings.ContainsAny(rest, " \t") {
				continue
			}
			return rest
		}
		return strings.TrimSpace(v)
	}
	return ""
}

// redactProxyKey returns a logging-safe prefix of a proxy_key. For
// len > 8 it returns the first 8 chars; for len ≤ 8 it returns ~half,
// so short inputs never leak in full even if a future key format
// permits them.
func redactProxyKey(proxyKey string) string {
	n := len(proxyKey)
	if n == 0 {
		return ""
	}
	if n > 8 {
		return proxyKey[:8]
	}
	return proxyKey[:(n+1)/2]
}

func clientIP(r *http.Request) string {
	// Identity is based on TCP source IP only (do not trust X-Forwarded-For).
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

func isTimeout(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return true
	}
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

// isRequestBodyTooLarge reports whether err originates from
// http.MaxBytesReader rejecting a body read on the request path. The error
// surfaces from http.Client.Do (and may be wrapped in *url.Error) when the
// transport tries to forward a body chunk past the configured cap.
func isRequestBodyTooLarge(err error) bool {
	if err == nil {
		return false
	}
	var mbe *http.MaxBytesError
	return errors.As(err, &mbe)
}

func clientMessage(code platform.Code) string {
	switch code {
	case platform.CodeBadRequest:
		return "bad request"
	case platform.CodeUnauthorized:
		return "unauthorized"
	case platform.CodeNotFound:
		return "not found"
	case platform.CodeBadGateway:
		return "bad gateway"
	case platform.CodeServiceUnavailable:
		return "service unavailable"
	case platform.CodeGatewayTimeout:
		return "gateway timeout"
	case platform.CodePayloadTooLarge:
		return "request body too large"
	case platform.CodeInternal:
		return "internal error"
	default:
		return http.StatusText(code.HTTPStatus())
	}
}

var hopByHopHeaders = []string{
	"Connection",
	"Proxy-Connection",
	"Keep-Alive",
	"Proxy-Authenticate",
	"Proxy-Authorization",
	"Te",
	"Trailer",
	"Transfer-Encoding",
	"Upgrade",
}

func stripHopByHopHeaders(h http.Header) {
	if c := h.Get("Connection"); c != "" {
		for _, f := range strings.Split(c, ",") {
			if name := strings.TrimSpace(f); name != "" {
				h.Del(name)
			}
		}
	}
	for _, name := range hopByHopHeaders {
		h.Del(name)
	}
}

func cloneHeader(h http.Header) http.Header {
	out := make(http.Header, len(h))
	for k, v := range h {
		vals := make([]string, len(v))
		copy(vals, v)
		out[k] = vals
	}
	return out
}

func copyHeader(dst, src http.Header) {
	for k, vv := range src {
		for _, v := range vv {
			dst.Add(k, v)
		}
	}
}

func (h *Handler) logReject(sourceIP, apiBase, reason string, code platform.Code) {
	slog.Warn("reject",
		"action", "reject",
		"source_ip", sourceIP,
		"api_base", apiBase,
		"reason", reason,
		"code", string(code),
		"status", code.HTTPStatus(),
	)
}
