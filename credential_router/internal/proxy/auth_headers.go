// Package proxy — single source of truth for standard LLM credential
// headers. Any new provider auth scheme belongs in this file.
package proxy

// Standard LLM credential headers. Add new providers here so handler
// and header_transform both stay in sync via the constants below.
const (
	HeaderAuthorization = "Authorization"
	HeaderAPIKey        = "X-Api-Key"
	HeaderGoogAPIKey    = "X-Goog-Api-Key"
)

// AuthRule defines how to inject the real credential for a given
// auth_type into the upstream request.
type AuthRule struct {
	Header string // HTTP header to write
	Prefix string // value prefix (e.g. "Bearer "); empty for raw-token headers
}

// authHeaderFamilies lists every credential-auth header we strip from
// the client request before InjectRealKey writes the real key. Without
// this, a client using OpenAI SDK (Authorization: Bearer) with a
// credential registered as anthropic would forward its proxy_key-
// bearing Authorization header verbatim to the upstream server.
//
// Adding a new provider whose SDK sets an auth header? Add the header
// here AND to authTypeMap.
var authHeaderFamilies = []string{
	HeaderAuthorization,
	HeaderAPIKey,
	HeaderGoogAPIKey,
}

// proxyKeySourceHeaders is the ordered list (highest priority first)
// of headers that may carry the proxy_key on incoming requests.
// Standard LLM SDKs set one of these three automatically; the proxy
// accepts any so callers don't need to customize SDK auth.
//
// Adding a new provider whose SDK sets an auth header that callers
// might want to carry proxy_key? Add it here in priority position
// AND to authHeaderFamilies (so it gets stripped before inject).
var proxyKeySourceHeaders = []string{
	HeaderAuthorization,
	HeaderAPIKey,
	HeaderGoogAPIKey,
}

// authTypeMap maps credential.auth_type to its upstream inject header
// and value prefix.
var authTypeMap = map[string]AuthRule{
	"openai":    {Header: HeaderAuthorization, Prefix: "Bearer "},
	"anthropic": {Header: HeaderAPIKey, Prefix: ""},
	"google":    {Header: HeaderGoogAPIKey, Prefix: ""},
}
