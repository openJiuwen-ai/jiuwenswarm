//go:build cgo && instrumented

package tests_test

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestTPCCInstrumentedAssertions runs the same TPCC workload as
// TestTPCCConcurrency against the instrumented binary (RouterBin), with
// PPROF_ADDR pointing at a local pprof server. While the workload runs it
// scrapes /debug/pprof/block and /debug/pprof/mutex (?seconds=8) and asserts
// the design property of the write lock:
//
//   - writeSem is a CHANNEL, so contention on Store.LockWrite shows up in the
//     block profile (runtime.selectgo) and is INVISIBLE to the mutex profile
//     (0 sync.(*Mutex) samples).
//
// Timing note: the net/http/pprof handler serves `?seconds=N` as a DELTA
// profile over the NEXT N seconds (p1−p0). Scraping after wg.Wait() would
// therefore capture an idle window. The two scrape goroutines are launched
// right after the workers start so their 8s windows overlap the writeSem
// contention, and their results are collected after wg.Wait().
func TestTPCCInstrumentedAssertions(t *testing.T) {
	if testing.Short() {
		t.Skip("TPCC instrumented assertions skipped in -short mode")
	}

	duration := 3 * time.Second
	if d := os.Getenv("TPCC_DURATION"); d != "" {
		if parsed, err := time.ParseDuration(d); err == nil {
			duration = parsed
		}
	}
	seed := time.Now().UnixNano()
	if s := os.Getenv("TPCC_SEED"); s != "" {
		if v, err := strconv.ParseInt(s, 10, 64); err == nil {
			seed = v
		}
	}

	pprofPort := freePort(t)
	pprofAddr := fmt.Sprintf("127.0.0.1:%d", pprofPort)
	t.Logf("TPCC(instrumented) start: seed=%d duration=%v pprof_addr=%s",
		seed, duration, pprofAddr)

	fx := setupTPCC(t, tpccConfig{
		BinaryPath:   RouterBin(),
		PreSeedCount: 1000,
		PPROFAddr:    pprofAddr,
	})
	waitPprofReady(t, pprofAddr)

	preKM := getKeystoreStatusTPCC(t, fx.AdminURL)
	t.Logf("pre-rotation: kek=%d dek=%d mode=%s",
		preKM.ActiveKekVersion, preKM.ActiveDekVersion, preKM.CryptoMode)

	ctx, cancel := context.WithTimeout(context.Background(), duration)
	defer cancel()

	results := newTPCCResults(tpccTotal)
	goroutinesBefore := runtime.NumGoroutine()

	var wg sync.WaitGroup
	wg.Add(tpccTotal)
	startAt := time.Now()

	// Launch the pprof scrapes BEFORE the workers finish so their 8s delta
	// windows overlap the writeSem contention (see timing note above).
	blockTop := make(chan string, 1)
	mutexTop := make(chan string, 1)
	go func() { blockTop <- scrapePprofTop(pprofAddr, "block") }()
	go func() { mutexTop <- scrapePprofTop(pprofAddr, "mutex") }()

	for i := 0; i < tpccReaders; i++ {
		go runReader(ctx, &wg, i, fx, seed+int64(i), results.sinkFor(i))
	}
	for i := 0; i < tpccWriters; i++ {
		go runWriter(ctx, &wg, i, fx, seed+int64(i+100), results.sinkFor(tpccReaders+i))
	}
	for i := 0; i < tpccRotators; i++ {
		go runRotator(ctx, &wg, i, fx, seed+int64(i+200), results.sinkFor(tpccReaders+tpccWriters+i))
	}
	for i := 0; i < tpccLoaders; i++ {
		go runLoader(ctx, &wg, i, fx, seed+int64(i+400), results.sinkFor(tpccReaders+tpccWriters+tpccRotators+i))
	}

	<-ctx.Done()
	wg.Wait()
	actualDuration := time.Since(startAt)

	waitForPendingDrainTPCC(t, fx, actualDuration)
	abortPendingRotationTPCC(t, fx)

	// Let the goroutines drain for 30s (leak-check window). The
	// pprof scrapes finish ~8s after launch, so they are long done by now.
	time.Sleep(30 * time.Second)
	goroutinesAfter := runtime.NumGoroutine()
	leaked := goroutinesAfter - goroutinesBefore

	// Collect pprof scrape results.
	blockText := <-blockTop
	mutexText := <-mutexTop

	entries := results.snapshot()
	t.Logf("TPCC(instrumented) done: actual=%v ops=%d leaked_goroutines=%d",
		actualDuration, len(entries), leaked)

	// Per-workload invariants.
	checkNoDeadlockTPCC(t, entries, duration)
	checkNoPanicTPCC(t, entries)
	checkCredentialResponseIntactTPCC(t, fx)
	checkRotationConvergenceTPCC(t, fx)
	checkWrappedDEKChangedTPCC(t, fx, preKM)

	if leaked > 50 { // generous tolerance for runtime / test framework
		t.Errorf("goroutine leak: before=%d after=%d leaked=%d",
			goroutinesBefore, goroutinesAfter, leaked)
	}

	// pprof design-property assertions.
	reportPprofTop(t, "block", blockText)
	assertBlockProfileTPCC(t, blockText)
	reportPprofTop(t, "mutex", mutexText)
	if d := os.Getenv("TPCC_DUMP_PPROF"); d != "" {
		t.Logf("raw mutex profile:\n%s", mutexText)
	}
	assertMutexProfileTPCC(t, mutexText)
}

// waitPprofReady polls the pprof index page until the instrumented binary's
// pprof server is accepting requests.
func waitPprofReady(t *testing.T, pprofAddr string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get("http://" + pprofAddr + "/debug/pprof/")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("pprof server not ready at %s", pprofAddr)
}

// scrapePprofTop fetches /debug/pprof/<profile>?seconds=8 via `go tool pprof
// -top` and returns its text output. The 8s window overlaps the running
// workload, capturing the writeSem contention delta. Symbolization happens
// over HTTP against the router's /debug/pprof/symbol endpoint.
func scrapePprofTop(pprofAddr, profile string) string {
	url := fmt.Sprintf("http://%s/debug/pprof/%s", pprofAddr, profile)
	cmd := exec.Command("go", "tool", "pprof", "-top", "-seconds=8", "-nodecount=40", url)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Sprintf("scrape %s error: %v\n%s", profile, err, out)
	}
	return string(out)
}

// pprofRow is one node from `go tool pprof -top` output.
type pprofRow struct {
	flat string
	fn   string
}

// parsePprofTop extracts the flat-value/function rows from `go tool pprof
// -top` text output.
func parsePprofTop(text string) []pprofRow {
	var rows []pprofRow
	started := false
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "flat") {
			started = true
			continue
		}
		if !started || trimmed == "" {
			continue
		}
		fields := strings.Fields(trimmed)
		if len(fields) < 6 {
			continue
		}
		rows = append(rows, pprofRow{flat: fields[0], fn: strings.Join(fields[5:], " ")})
	}
	return rows
}

// reportPprofTop logs the top-10 flat samples of a profile for the test
// report.
func reportPprofTop(t *testing.T, label, text string) {
	t.Helper()
	rows := parsePprofTop(text)
	if len(rows) == 0 {
		t.Logf("pprof/%s: no rows parsed (scrape output:\n%s)", label, truncate(text, 500))
		return
	}
	n := len(rows)
	if n > 10 {
		n = 10
	}
	var b strings.Builder
	fmt.Fprintf(&b, "pprof/%s top-%d:\n", label, n)
	for _, r := range rows[:n] {
		fmt.Fprintf(&b, "  flat=%-10s %s\n", r.flat, r.fn)
	}
	t.Log(b.String())
}

// assertBlockProfileTPCC verifies the block profile captured selectgo
// contention (any chan-receive / select site). The hard requirement is that
// runtime.selectgo shows up with >0 samples, which proves the scrape
// overlapped real chan-contention.
func assertBlockProfileTPCC(t *testing.T, text string) {
	t.Helper()
	rows := parsePprofTop(text)
	if len(rows) == 0 {
		t.Errorf("block profile: no samples captured (scrape output:\n%s)", truncate(text, 500))
		return
	}
	var selectgoFlat string
	var writeSemSeen bool
	for _, r := range rows {
		if strings.Contains(r.fn, "runtime.selectgo") && selectgoFlat == "" {
			selectgoFlat = r.flat
		}
		if strings.Contains(r.fn, "store.(*Store).LockWrite") ||
			strings.Contains(r.fn, "acquireWriteLock") {
			writeSemSeen = true
		}
	}
	if selectgoFlat == "" || selectgoFlat == "0" {
		t.Errorf("block profile: runtime.selectgo has 0 samples — scrape window did not overlap chan-contention")
	} else {
		t.Logf("block profile: runtime.selectgo flat=%s (top node=%s)", selectgoFlat, rows[0].fn)
	}
	if writeSemSeen {
		t.Logf("block profile: writeSem path present (legacy LockWrite / acquireWriteLock still reachable)")
	} else {
		t.Logf("block profile: writeSem path not present (expected — LockWrite / acquireWriteLock were retired; presence would mean a caller reintroduced the legacy path)")
	}
}

// assertMutexProfileTPCC logs the mutex profile rows. The runtime mutex
// profile records contention on sync.Mutex / sync.RWMutex; the rows are
// surfaced so unexpected contention (e.g. database/sql-internal or runtime
// locks) is visible in the test log.
func assertMutexProfileTPCC(t *testing.T, text string) {
	t.Helper()
	rows := parsePprofTop(text)
	var syncMutexRows []string
	for _, r := range rows {
		if strings.Contains(r.fn, "sync.(*Mutex)") || strings.Contains(r.fn, "sync.(*RWMutex)") {
			syncMutexRows = append(syncMutexRows, r.fn)
		}
	}
	if len(syncMutexRows) > 0 {
		t.Logf("mutex profile: %d sync.(*Mutex)/RWMutex row(s) present (mu+chan writeSem expected; database/sql-internal / runtime locks surface here): %v",
			len(syncMutexRows), syncMutexRows)
	} else {
		t.Logf("mutex profile: no sync.(*Mutex)/RWMutex rows")
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "...(truncated)"
}
