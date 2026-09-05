//go:build cgo

package tests_test

import (
	"context"
	"os"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"
)

// TestTPCCAgainstProductionBinary exercises the same TPCC workload against a
// production build (no -tags instrumented) to prove the production binary
// behaves identically to the instrumented one for basic concurrency
// invariants.
//
// Always compiled. Runs alongside TestTPCCConcurrency (instrumented binary).
func TestTPCCAgainstProductionBinary(t *testing.T) {
	if testing.Short() {
		t.Skip("TPCC against production binary skipped in -short mode")
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
	t.Logf("TPCC(production) start: seed=%d duration=%v", seed, duration)

	fx := setupTPCC(t, tpccConfig{
		BinaryPath:   ProductionBin(),
		PreSeedCount: 1000,
	})

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
	time.Sleep(30 * time.Second)
	goroutinesAfter := runtime.NumGoroutine()
	leaked := goroutinesAfter - goroutinesBefore

	entries := results.snapshot()
	t.Logf("TPCC(production) done: actual=%v ops=%d leaked=%d", actualDuration, len(entries), leaked)

	checkNoDeadlockTPCC(t, entries, duration)
	checkNoPanicTPCC(t, entries)
	checkCredentialResponseIntactTPCC(t, fx)
	checkRotationConvergenceTPCC(t, fx)
	checkWrappedDEKChangedTPCC(t, fx, preKM)

	if leaked > 50 {
		t.Errorf("goroutine leak: before=%d after=%d leaked=%d", goroutinesBefore, goroutinesAfter, leaked)
	}
}
