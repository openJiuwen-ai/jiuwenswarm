//go:build cgo

package tests_test

import (
	"context"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/http/httptrace"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestBenchProxyVsDirectStub measures credential-router proxy overhead by
// running the same workload twice: once with clients hitting a local stub
// upstream HTTP server directly (baseline, no proxy overhead), and once
// with clients hitting the same stub THROUGH the credential router.
//
// Both paths run against the PRODUCTION binary (ProductionBin) — RouterBin is
// instrumented with pprof overhead that would pollute the measurement.
//
// When BENCH_STREAM=1 the same workload also runs in streaming (SSE) mode
// (direct stub SSE, then router SSE).
//
// Streaming modes measure time to first byte (TTFB, via
// httptrace.ClientTrace.GotFirstResponseByte), total stream time, bytes
// received, and chunk count (number of body Read calls with n>0).
//
// Env knobs (all optional, defaults in parentheses):
//
//	BENCH_DURATION      measurement window per mode (30s)
//	BENCH_CONCURRENCY   concurrent client goroutines (10)
//	BENCH_CRED_COUNT    credentials seeded into the router (50)
//	BENCH_WARMUP        warm-up window before measurement (2s)
//	BENCH_STREAM        1 = also run streaming modes (default 0 = single-shot only)
//	BENCH_STREAM_DELAY  stub sleep between SSE chunks (0s = instant)
//	BENCH_STREAM_CHUNKS number of SSE chunks per stream (10)
func TestBenchProxyVsDirectStub(t *testing.T) {
	if testing.Short() {
		t.Skip("benchmark skipped in -short mode")
	}

	duration := benchDuration(t)
	concurrency := benchConcurrency(t)
	credCount := benchCredCount(t)
	warmup := benchWarmup(t)
	stream := benchStreamEnabled(t)
	streamDelay := benchStreamDelay(t)
	streamChunks := benchStreamChunks(t)
	streamFlag := 0
	if stream {
		streamFlag = 1
	}
	t.Logf("bench config: duration=%v concurrency=%d cred_count=%d warmup=%v stream=%d stream_delay=%v stream_chunks=%d",
		duration, concurrency, credCount, warmup, streamFlag, streamDelay, streamChunks)

	// 1. Stub upstream. Manual net.Listen + http.Serve (NOT httptest, which
	//    wraps the handler and changes URL semantics). Plain GETs respond
	//    instantly with 200 + ~1KB JSON so latency reflects router overhead,
	//    not stub processing. GETs carrying ?stream=1 get an SSE response
	//    (text/event-stream, delay between chunks) instead.
	stubBaseURL := startBenchStub(t, streamChunks, streamDelay)

	// 2. Seed credentials and start the production router binary.
	creds := make([]credentialEntry, 0, credCount)
	for i := 0; i < credCount; i++ {
		creds = append(creds, credentialEntry{
			UserID:   "bench-user",
			APIBase:  stubBaseURL,
			KeyTag:   fmt.Sprintf("bench-%d", i),
			APIKey:   fmt.Sprintf("sk-bench-key-%d", i),
			AuthType: "openai",
		})
	}
	router := startRouterWithBin(t, routerConfig{Credentials: creds}, benchRouterBin())

	// Proxy keys are server-generated at create time, so read them back via
	// the admin list endpoint. The proxy URL is shared across credentials
	// (all point at the same stub api_base).
	proxyKeys := make([]string, credCount)
	for i := 0; i < credCount; i++ {
		proxyKeys[i] = proxyKeyFor(t, router, "", stubBaseURL, fmt.Sprintf("bench-%d", i))
	}
	routerProxyURL := proxyURL(router.BaseURL, stubBaseURL, "/v1/chat/completions")

	// 3. Warm the router cache: one request per credential through the proxy.
	//    The first request for a credential triggers a DB read; subsequent
	//    ones hit the in-memory cache, so this must happen before measuring.
	warmClient := benchHTTPClient()
	for i := 0; i < credCount; i++ {
		req, _ := http.NewRequest("GET", routerProxyURL, nil)
		req.Header.Set("Authorization", "Bearer "+proxyKeys[i])
		resp, err := warmClient.Do(req)
		if err != nil {
			t.Fatalf("cache warm-up request %d: %v", i, err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("cache warm-up request %d: status %d", i, resp.StatusCode)
		}
	}
	warmClient.CloseIdleConnections()

	// 4. Direct stub baseline (single-shot).
	dummyAuths := make([]string, credCount)
	for i := 0; i < credCount; i++ {
		dummyAuths[i] = "Bearer bench-dummy-key-" + strconv.Itoa(i)
	}
	directURL := stubBaseURL + "/v1/chat/completions"
	directSS := runBenchMode(benchMode{
		name:        "direct stub (single-shot)",
		url:         directURL,
		auths:       dummyAuths,
		credCount:   credCount,
		concurrency: concurrency,
		duration:    duration,
		warmup:      warmup,
		client:      benchHTTPClient(),
		seed:        time.Now().UnixNano(),
	})

	// 5. Through credential router (single-shot).
	realAuths := make([]string, credCount)
	for i := 0; i < credCount; i++ {
		realAuths[i] = "Bearer " + proxyKeys[i]
	}
	routerSS := runBenchMode(benchMode{
		name:        "through credential router (single-shot)",
		url:         routerProxyURL,
		auths:       realAuths,
		credCount:   credCount,
		concurrency: concurrency,
		duration:    duration,
		warmup:      warmup,
		client:      benchHTTPClient(),
		seed:        time.Now().UnixNano(),
	})

	all := []benchStats{directSS, routerSS}

	// 6. Optional streaming modes (BENCH_STREAM=1): same URLs with ?stream=1
	//    so the stub answers with SSE. Each mode gets its own http.Client so
	//    connections are never reused across modes.
	if stream {
		directStream := runBenchMode(benchMode{
			name:        "direct stub (streaming)",
			url:         directURL + "?stream=1",
			auths:       dummyAuths,
			credCount:   credCount,
			concurrency: concurrency,
			duration:    duration,
			warmup:      warmup,
			client:      benchHTTPClient(),
			seed:        time.Now().UnixNano(),
			stream:      true,
		})
		routerStream := runBenchMode(benchMode{
			name:        "through credential router (streaming)",
			url:         routerProxyURL + "?stream=1",
			auths:       realAuths,
			credCount:   credCount,
			concurrency: concurrency,
			duration:    duration,
			warmup:      warmup,
			client:      benchHTTPClient(),
			seed:        time.Now().UnixNano(),
			stream:      true,
		})

		// 7. Report all four modes + per-mode-type deltas.
		printBenchStats(t, directSS)
		printBenchStats(t, routerSS)
		printBenchStats(t, directStream)
		printBenchStats(t, routerStream)
		printBenchDelta(t, directSS, routerSS, &directStream, &routerStream)
		all = append(all, directStream, routerStream)
	} else {
		// 7. Report single-shot modes + delta.
		printBenchStats(t, directSS)
		printBenchStats(t, routerSS)
		printBenchDelta(t, directSS, routerSS, nil, nil)
	}

	// 8. Surface the first few unique errors as warnings (never abort).
	for _, s := range all {
		if len(s.errs) == 0 {
			continue
		}
		t.Logf("Mode %s: %d errors (first %d unique):", s.name, s.errors, minInt(5, len(s.errs)))
		n := 0
		for msg, count := range s.errs {
			if n >= 5 {
				break
			}
			t.Logf("  [x%d] %s", count, msg)
			n++
		}
	}
}

// --- Env knobs ---

func benchRouterBin() string {
	if os.Getenv("BENCH_PROFILE") == "1" {
		return RouterBin()
	}
	return ProductionBin()
}

func benchDuration(t *testing.T) time.Duration {
	t.Helper()
	d := 30 * time.Second
	if v := os.Getenv("BENCH_DURATION"); v != "" {
		if parsed, err := time.ParseDuration(v); err == nil && parsed > 0 {
			d = parsed
		} else {
			t.Logf("BENCH_DURATION=%q ignored (want positive duration)", v)
		}
	}
	return d
}

func benchConcurrency(t *testing.T) int {
	t.Helper()
	n := 10
	if v := os.Getenv("BENCH_CONCURRENCY"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			n = parsed
		} else {
			t.Logf("BENCH_CONCURRENCY=%q ignored (want positive int)", v)
		}
	}
	return n
}

func benchCredCount(t *testing.T) int {
	t.Helper()
	n := 50
	if v := os.Getenv("BENCH_CRED_COUNT"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			n = parsed
		} else {
			t.Logf("BENCH_CRED_COUNT=%q ignored (want positive int)", v)
		}
	}
	return n
}

func benchWarmup(t *testing.T) time.Duration {
	t.Helper()
	d := 2 * time.Second
	if v := os.Getenv("BENCH_WARMUP"); v != "" {
		if parsed, err := time.ParseDuration(v); err == nil && parsed >= 0 {
			d = parsed
		} else {
			t.Logf("BENCH_WARMUP=%q ignored (want non-negative duration)", v)
		}
	}
	return d
}

func benchStreamEnabled(t *testing.T) bool {
	t.Helper()
	if v := os.Getenv("BENCH_STREAM"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			return true
		}
		t.Logf("BENCH_STREAM=%q ignored (want 0 or 1)", v)
	}
	return false
}

func benchStreamDelay(t *testing.T) time.Duration {
	t.Helper()
	d := 0 * time.Second
	if v := os.Getenv("BENCH_STREAM_DELAY"); v != "" {
		if parsed, err := time.ParseDuration(v); err == nil && parsed >= 0 {
			d = parsed
		} else {
			t.Logf("BENCH_STREAM_DELAY=%q ignored (want non-negative duration)", v)
		}
	}
	return d
}

func benchStreamChunks(t *testing.T) int {
	t.Helper()
	n := 10
	if v := os.Getenv("BENCH_STREAM_CHUNKS"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil && parsed > 0 {
			n = parsed
		} else {
			t.Logf("BENCH_STREAM_CHUNKS=%q ignored (want positive int)", v)
		}
	}
	return n
}

// --- Stub upstream ---

// startBenchStub binds a plain HTTP server to 127.0.0.1:<free-port> and
// returns its base URL. Plain GETs get an instant 200 + ~1KB JSON body; GETs
// with ?stream=1 get an SSE response (text/event-stream) of `chunks` events
// separated by `delay`, each flushed immediately.
func startBenchStub(t *testing.T, chunks int, delay time.Duration) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("bench stub listen: %v", err)
	}
	body := benchStubBody()
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("stream") == "1" {
			benchStubStream(w, r, chunks, delay)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return "http://" + ln.Addr().String()
}

// benchStubStream writes an SSE response of `chunks` events. Each event is a
// `data: {...}` line followed by a blank line, matching OpenAI-style chat
// completion streams. When delay > 0 the handler sleeps between events to
// simulate a slow model.
func benchStubStream(w http.ResponseWriter, r *http.Request, chunks int, delay time.Duration) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)
	for i := 0; i < chunks; i++ {
		select {
		case <-r.Context().Done():
			return
		default:
		}
		if delay > 0 {
			time.Sleep(delay)
		}
		_, _ = fmt.Fprintf(w, "data: {\"index\":%d,\"content\":\"chunk-%d\"}\n\n", i, i)
		flusher.Flush()
	}
}

func benchStubBody() []byte {
	content := strings.Repeat("x", 900)
	return []byte(fmt.Sprintf(
		`{"ok":true,"model":"bench-stub","choices":[{"index":0,"message":{"role":"assistant","content":%q}}]}`,
		content))
}

// benchHTTPClient returns a keep-alive-enabled client tuned for concurrency.
// Modes must NOT share a client so connections are not reused across modes.
func benchHTTPClient() *http.Client {
	transport := &http.Transport{
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 200,
		MaxConnsPerHost:     200,
		IdleConnTimeout:     30 * time.Second,
		DisableKeepAlives:   false,
	}
	return &http.Client{Transport: transport}
}

// --- Measurement harness ---

type benchMode struct {
	name        string
	url         string
	auths       []string // Authorization header value per credential index
	credCount   int
	concurrency int
	duration    time.Duration
	warmup      time.Duration
	client      *http.Client
	seed        int64
	stream      bool // SSE mode: measure TTFB + full stream time + bytes/chunks
}

type benchStats struct {
	name      string
	requests  int64
	errors    int64
	duration  time.Duration
	latencies []int64 // microseconds (single-shot modes)
	ttfb      []int64 // microseconds, time to first byte (streaming modes)
	totalStreamTime []int64 // microseconds, full stream duration (streaming modes)
	bytesRecv int64 // total body bytes received (streaming modes)
	chunkCount int64 // total body Read calls with n>0 (streaming modes)
	errs      map[string]int

	rps   float64 // req/s
	min   float64 // ms
	p50   float64 // ms
	p95   float64 // ms
	p99   float64 // ms
	max   float64 // ms
	mean  float64 // ms
	stdev float64 // ms

	// Streaming-mode stats (ms), populated only when ttfb/totalStreamTime
	// are non-empty.
	ttfbMin   float64
	ttfbP50   float64
	ttfbP95   float64
	ttfbP99   float64
	ttfbMax   float64
	ttfbMean  float64
	ttfbStdev float64

	streamMin   float64
	streamP50   float64
	streamP95   float64
	streamP99   float64
	streamMax   float64
	streamMean  float64
	streamStdev float64
}

// benchRunState holds the shared measurement state across the goroutines of a
// single bench mode: atomic counters, mutex-guarded sample slices, and the
// error map.
type benchRunState struct {
	reqCount  atomic.Int64
	errCount  atomic.Int64
	measuring atomic.Bool

	latMu     sync.Mutex
	latencies []int64 // single-shot latencies, microseconds

	sampleMu      sync.Mutex
	ttfbSamples   []int64 // microseconds
	streamSamples []int64 // microseconds

	bytesRecv  atomic.Int64
	chunkTotal atomic.Int64

	errMu   sync.Mutex
	errSeen map[string]int
}

// runBenchMode drives `concurrency` goroutines against url for `duration`,
// preceded by a `warmup` window that sends requests without recording them.
// A channel-based start gate plus a ready barrier launches all goroutines
// simultaneously; an atomic.Bool gates measurement on/off. In stream mode each
// request additionally records TTFB and full stream time.
func runBenchMode(m benchMode) benchStats {
	st := &benchRunState{errSeen: make(map[string]int)}

	startGate := make(chan struct{})
	stopCh := make(chan struct{})
	var readyWg sync.WaitGroup
	readyWg.Add(m.concurrency)
	var wg sync.WaitGroup
	wg.Add(m.concurrency)

	for g := 0; g < m.concurrency; g++ {
		go func(g int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(m.seed + int64(g)))
			readyWg.Done()
			<-startGate
			for {
				select {
				case <-stopCh:
					return
				default:
				}
				i := rng.Intn(m.credCount)
				if m.stream {
					benchStreamRequest(m, i, st)
				} else {
					benchSingleRequest(m, i, st)
				}
			}
		}(g)
	}

	readyWg.Wait()
	close(startGate)
	time.Sleep(m.warmup)
	st.measuring.Store(true)
	time.Sleep(m.duration)
	st.measuring.Store(false)
	close(stopCh)
	wg.Wait()

	res := benchStats{
		name:            m.name,
		requests:        st.reqCount.Load(),
		errors:          st.errCount.Load(),
		duration:        m.duration,
		latencies:       st.latencies,
		ttfb:            st.ttfbSamples,
		totalStreamTime: st.streamSamples,
		bytesRecv:       st.bytesRecv.Load(),
		chunkCount:      st.chunkTotal.Load(),
		errs:            st.errSeen,
	}
	res.compute()
	return res
}

// benchSingleRequest issues one single-shot GET and, while measuring, records
// its latency on success (status < 400).
func benchSingleRequest(m benchMode, i int, st *benchRunState) {
	req, err := http.NewRequest("GET", m.url, nil)
	if err != nil {
		if st.measuring.Load() {
			st.reqCount.Add(1)
			st.errCount.Add(1)
			recordBenchErr(&st.errMu, st.errSeen, err)
		}
		return
	}
	req.Header.Set("Authorization", m.auths[i])
	start := time.Now()
	resp, err := m.client.Do(req)
	lat := time.Since(start)
	if err != nil {
		if st.measuring.Load() {
			st.reqCount.Add(1)
			st.errCount.Add(1)
			recordBenchErr(&st.errMu, st.errSeen, err)
		}
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if !st.measuring.Load() {
		return
	}
	st.reqCount.Add(1)
	if resp.StatusCode >= 400 {
		st.errCount.Add(1)
		recordBenchErr(&st.errMu, st.errSeen, fmt.Errorf("status %d", resp.StatusCode))
		return
	}
	st.latMu.Lock()
	st.latencies = append(st.latencies, lat.Microseconds())
	st.latMu.Unlock()
}

// benchStreamRequest issues one streaming GET, measuring TTFB via
// httptrace.ClientTrace.GotFirstResponseByte, then drains the body counting
// bytes and Read calls. While measuring, it records TTFB + full stream time on
// success (status < 400 and body fully drained without read errors). Body read
// errors after the first byte count as request errors.
func benchStreamRequest(m benchMode, i int, st *benchRunState) {
	req, err := http.NewRequest("GET", m.url, nil)
	if err != nil {
		if st.measuring.Load() {
			st.reqCount.Add(1)
			st.errCount.Add(1)
			recordBenchErr(&st.errMu, st.errSeen, err)
		}
		return
	}
	req.Header.Set("Authorization", m.auths[i])
	var ttfb atomic.Int64
	start := time.Now()
	trace := &httptrace.ClientTrace{
		GotFirstResponseByte: func() { ttfb.Store(int64(time.Since(start))) },
	}
	req = req.WithContext(httptrace.WithClientTrace(context.Background(), trace))
	resp, err := m.client.Do(req)
	if err != nil {
		if st.measuring.Load() {
			st.reqCount.Add(1)
			st.errCount.Add(1)
			recordBenchErr(&st.errMu, st.errSeen, err)
		}
		return
	}
	var (
		totalBytes int64
		chunkCount int64
		readErr    error
	)
	buf := make([]byte, 4096)
	streamStart := time.Now()
	for {
		n, rerr := resp.Body.Read(buf)
		totalBytes += int64(n)
		if n > 0 {
			chunkCount++
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			readErr = rerr
			break
		}
	}
	resp.Body.Close()
	streamDur := time.Since(streamStart)
	if !st.measuring.Load() {
		return
	}
	st.reqCount.Add(1)
	if resp.StatusCode >= 400 {
		st.errCount.Add(1)
		recordBenchErr(&st.errMu, st.errSeen, fmt.Errorf("status %d", resp.StatusCode))
		return
	}
	if readErr != nil {
		st.errCount.Add(1)
		recordBenchErr(&st.errMu, st.errSeen, readErr)
		return
	}
	st.bytesRecv.Add(totalBytes)
	st.chunkTotal.Add(chunkCount)
	st.sampleMu.Lock()
	st.ttfbSamples = append(st.ttfbSamples, ttfb.Load()/1000) // ns → µs
	st.streamSamples = append(st.streamSamples, streamDur.Microseconds())
	st.sampleMu.Unlock()
}

func recordBenchErr(mu *sync.Mutex, seen map[string]int, err error) {
	mu.Lock()
	seen[err.Error()]++
	mu.Unlock()
}

// --- Stats ---

func (s *benchStats) compute() {
	if len(s.latencies) > 0 {
		s.min, s.p50, s.p95, s.p99, s.max, s.mean, s.stdev = benchDistMs(s.latencies)
	}
	if len(s.ttfb) > 0 {
		s.ttfbMin, s.ttfbP50, s.ttfbP95, s.ttfbP99, s.ttfbMax, s.ttfbMean, s.ttfbStdev = benchDistMs(s.ttfb)
	}
	if len(s.totalStreamTime) > 0 {
		s.streamMin, s.streamP50, s.streamP95, s.streamP99, s.streamMax, s.streamMean, s.streamStdev = benchDistMs(s.totalStreamTime)
	}
	s.rps = float64(s.requests) / s.duration.Seconds()
}

// benchDistMs sorts a slice of microsecond samples in place and returns
// min/p50/p95/p99/max/mean/stdev in milliseconds.
func benchDistMs(sorted []int64) (min, p50, p95, p99, max, mean, stdev float64) {
	n := len(sorted)
	if n == 0 {
		return 0, 0, 0, 0, 0, 0, 0
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	min = float64(sorted[0]) / 1000.0
	max = float64(sorted[n-1]) / 1000.0
	p50 = benchPercentileMs(sorted, 0.50)
	p95 = benchPercentileMs(sorted, 0.95)
	p99 = benchPercentileMs(sorted, 0.99)
	var sum int64
	for _, l := range sorted {
		sum += l
	}
	mean = float64(sum) / float64(n) / 1000.0
	var sq float64
	for _, l := range sorted {
		d := float64(l)/1000.0 - mean
		sq += d * d
	}
	stdev = math.Sqrt(sq / float64(n))
	return min, p50, p95, p99, max, mean, stdev
}

func benchPercentileMs(sorted []int64, p float64) float64 {
	idx := int(float64(len(sorted)) * p)
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return float64(sorted[idx]) / 1000.0
}

// --- Reporting ---

func printBenchStats(t *testing.T, s benchStats) {
	t.Logf("=== Mode %s ===", s.name)
	t.Logf("  Requests:    %d  errors: %d", s.requests, s.errors)
	t.Logf("  Duration:    %.1fs  RPS: %.1f", s.duration.Seconds(), s.rps)
	if len(s.latencies) > 0 {
		t.Logf("  Latency (ms): min=%.3f p50=%.3f p95=%.3f p99=%.3f max=%.3f mean=%.3f stdev=%.3f",
			s.min, s.p50, s.p95, s.p99, s.max, s.mean, s.stdev)
	}
	if len(s.ttfb) > 0 {
		t.Logf("  TTFB (ms):    min=%.3f p50=%.3f p95=%.3f p99=%.3f max=%.3f mean=%.3f stdev=%.3f",
			s.ttfbMin, s.ttfbP50, s.ttfbP95, s.ttfbP99, s.ttfbMax, s.ttfbMean, s.ttfbStdev)
		t.Logf("  Stream (ms):  min=%.3f p50=%.3f p95=%.3f p99=%.3f max=%.3f mean=%.3f stdev=%.3f",
			s.streamMin, s.streamP50, s.streamP95, s.streamP99, s.streamMax, s.streamMean, s.streamStdev)
		avgBytes, avgChunks := int64(0), int64(0)
		if s.requests > 0 {
			avgBytes = s.bytesRecv / s.requests
			avgChunks = s.chunkCount / s.requests
		}
		t.Logf("  Bytes: %d (avg %d/req)  Chunks: %d (avg %d/req)",
			s.bytesRecv, avgBytes, s.chunkCount, avgChunks)
	}
}

// printBenchDelta reports router overhead for single-shot (a1 vs b1) and,
// when a2/b2 are non-nil, for streaming (a2 vs b2).
func printBenchDelta(t *testing.T, a1, b1 benchStats, a2, b2 *benchStats) {
	t.Logf("=== Delta (router overhead) ===")
	t.Logf("  single-shot:")
	t.Logf("    p50:    %+.3f ms", b1.p50-a1.p50)
	t.Logf("    p95:    %+.3f ms", b1.p95-a1.p95)
	t.Logf("    p99:    %+.3f ms", b1.p99-a1.p99)
	t.Logf("    mean:   %+.3f ms", b1.mean-a1.mean)
	printRPSDelta(t, b1, a1)
	if a2 != nil && b2 != nil {
		t.Logf("  streaming:")
		t.Logf("    TTFB p50:  %+.3f ms", b2.ttfbP50-a2.ttfbP50)
		t.Logf("    TTFB p95:  %+.3f ms", b2.ttfbP95-a2.ttfbP95)
		t.Logf("    TTFB p99:  %+.3f ms", b2.ttfbP99-a2.ttfbP99)
		t.Logf("    TTFB mean: %+.3f ms", b2.ttfbMean-a2.ttfbMean)
		t.Logf("    Stream mean: %+.3f ms", b2.streamMean-a2.streamMean)
		printRPSDelta(t, *b2, *a2)
	}
}

func printRPSDelta(t *testing.T, b, a benchStats) {
	rpsDelta := b.rps - a.rps
	pct := 0.0
	if a.rps > 0 {
		pct = rpsDelta / a.rps * 100.0
	}
	t.Logf("    RPS:    %+.1f (%+.1f%%)", rpsDelta, pct)
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
