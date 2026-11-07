package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"

	"credential_router/internal/credmgr"
	"credential_router/internal/credmgr/admin"
	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/cache"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	"credential_router/internal/platform"
	"credential_router/internal/proxy"
)

var version = "dev"

func main() {
	os.Exit(run())
}

func run() int {
	configPath := flag.String("config", "", "path to config YAML file")
	showVersion := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return 0
	}

	slogLevel := slog.LevelInfo
	if v := os.Getenv("LOG_LEVEL"); v != "" {
		switch strings.ToLower(v) {
		case "debug":
			slogLevel = slog.LevelDebug
		case "info":
			slogLevel = slog.LevelInfo
		case "warn", "warning":
			slogLevel = slog.LevelWarn
		case "error":
			slogLevel = slog.LevelError
		default:
			fmt.Fprintf(os.Stderr, "invalid LOG_LEVEL=%q (want debug|info|warn|error); using info\n", v)
		}
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slogLevel})))

	cfg, err := platform.Load(*configPath)
	if err != nil {
		slog.Error("load config failed", "error", err)
		return 1
	}

	if cfg.Server.LogFile != "" {
		logFile, err := os.OpenFile(cfg.Server.LogFile, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			slog.Error("open log file", "path", cfg.Server.LogFile, "error", err)
			return 1
		}
		defer logFile.Close()
		slog.SetDefault(slog.New(slog.NewJSONHandler(io.MultiWriter(os.Stdout, logFile), &slog.HandlerOptions{Level: slogLevel})))
	}

	// Ensure data_dir, secrets subdir, and backup_dir all exist. The store
	// and keystore handle their own files inside these directories.
	if err := os.MkdirAll(cfg.DataDir, 0o755); err != nil {
		slog.Error("create data dir", "path", cfg.DataDir, "error", err)
		return 1
	}
	if err := os.MkdirAll(cfg.SecretsDir(), 0o700); err != nil {
		slog.Error("create secrets dir", "path", cfg.SecretsDir(), "error", err)
		return 1
	}
	if err := os.MkdirAll(cfg.BackupDir, 0o755); err != nil {
		slog.Error("create backup dir", "path", cfg.BackupDir, "error", err)
		return 1
	}
	// Single-instance guard: hold .lock for the entire process lifetime.
	lockPath := filepath.Join(cfg.DataDir, ".lock")
	lockFile, err := os.OpenFile(lockPath, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		slog.Error("open lock file", "path", lockPath, "error", err)
		return 1
	}
	if err := platform.AcquireSingleInstanceLock(lockFile); err != nil {
		lockFile.Close()
		slog.Error("acquire single-instance lock", "path", lockPath, "error", err)
		return 78
	}
	pidPath := filepath.Join(cfg.DataDir, "router.pid")
	if err := platform.WritePIDFile(pidPath, os.Getpid()); err != nil {
		slog.Error("write pid file", "path", pidPath, "error", err)
		_ = platform.UnlockFileLock(lockFile)
		lockFile.Close()
		return 1
	}
	defer func() {
		_ = os.Remove(pidPath)
		_ = platform.UnlockFileLock(lockFile)
		lockFile.Close()
	}()

	st, err := store.OpenWithConfig(store.OpenConfig{
		Path: cfg.DBPath(),
	})
	if err != nil {
		slog.Error("open store failed", "path", cfg.DBPath(), "error", err)
		return 1
	}
	defer st.Close()

	bm, err := backup.NewBackupManager(backup.BackupConfig{
		BackupDir:   cfg.BackupDir,
		Keep:        cfg.Backup.Keep,
		FilenameTpl: cfg.Backup.FilenameTpl,
		KeySnapshot: backup.KeySnapshotConfig{
			Enabled:     cfg.Backup.KeySnapshot.Enabled,
			FilenameTpl: cfg.Backup.KeySnapshot.FilenameTpl,
			Keep:        cfg.Backup.KeySnapshot.Keep,
		},
	}, st)
	if err != nil {
		slog.Error("backup config invalid", "error", err)
		return 1
	}

	res, mgr, err := bootstrapKeystore(cfg, st, bm)
	if err != nil {
		if errors.Is(err, keystore.ErrStartupFatal) {
			slog.Error("bootstrap keystore FATAL — data corruption detected; DO NOT restart without restoring backup", "error", err)
			return 78
		}
		if errors.Is(err, keystore.ErrStartupRefused) {
			slog.Error("bootstrap keystore refused — fix config and restart", "error", err)
			return 78
		}
		slog.Error("bootstrap keystore failed", "error", err)
		return 1
	}
	// Zero key material on every exit path from here on. LIFO defer order:
	// stopAutoRotate → convergeCancel → ZeroAll (here) → shutdownZeroCancel → st.Close → pid/lock.
	// Scrub runs before st.Close so plaintext keys are wiped before releasing the DB handle.
	// mgr is non-nil past the bootstrap check above. Drain budget is bounded so a stuck inflight
	// holder cannot delay shutdown indefinitely; on drain timeout we still zero what we can
	// and return an error to slog.
	shutdownZeroCtx, shutdownZeroCancel := context.WithTimeout(context.Background(), cfg.Server.ShutdownZeroBudget)
	defer shutdownZeroCancel()
	defer func() {
		if err := mgr.ZeroAll(shutdownZeroCtx); err != nil {
			slog.Error("zero key material", "error", err)
		}
	}()

	rot := keystore.NewRotator(mgr, st, bm, cfg.SecretsDir())

	convergeCtx, convergeCancel := context.WithTimeout(context.Background(), cfg.Recovery.MaxWait)
	defer convergeCancel()
	// Run Phase A in the background so the HTTP servers bind immediately.
	// Concurrent safety during convergence: store row_version guards, Manager.mu
	// (serialises InstallDualSnap / ClearPrevious vs Capture / Swap), and the
	// Dual-snapshot fallback for vault reads: the Manager keeps both the
	// current and previous DEK snapshots live while a rotation is in flight,
	// so reads can decrypt rows encrypted with either version. RecoveryCase1Clean
	// returns nil immediately so the goroutine exits without work.
	go func() {
		if err := rot.RunStartupConvergence(convergeCtx, res, cfg.Recovery.MaxWait, int64(keystore.MaxRowsPerTx), cfg.Rotation.MaxPhaseALoops); err != nil {
			if errors.Is(err, keystore.ErrRotationInProgress) {
				slog.Error("startup convergence refused — rotation already in progress", "error", err)
				return
			}
			if errors.Is(err, store.ErrPhaseADecryptFail) {
				slog.Error("startup convergence FATAL — data corruption detected; DO NOT restart without restoring backup", "error", err)
				return
			}
			slog.Error("startup convergence failed", "error", err)
			return
		}
		slog.Info("startup convergence complete")
	}()

	cacheConfig := cache.Config{
		MaxEntries:   cfg.Cache.MaxEntries,
		TombstoneTTL: cfg.Cache.TombstoneTTL,
		EntryTTL:     cfg.Cache.EntryTTL,
	}
	credCache := cache.NewInMemoryCredentialCache(cacheConfig)

	ccg := cache.NewCachedCredentialGetter(credCache, credmgr.NewCredMgr(st, mgr))

	proxyHandler, err := proxy.NewHandler(cfg, ccg)
	if err != nil {
		slog.Error("create proxy handler failed", "error", err)
		return 1
	}

	adminServer := admin.NewServer(cfg.Admin, cfg.SSRF, cfg.Server, cfg.Rotation, cfg.Recovery, st, ccg, rot)
	proxyMux := http.NewServeMux()
	proxyMux.Handle("/", proxyHandler)

	adminMux := http.NewServeMux()
	adminMux.Handle("/v1/", adminServer.Routes())

	autoCtx, autoCancel := context.WithCancel(context.Background())
	stopAutoRotate := rot.StartAutoRotate(autoCtx, cfg.Rotation.Period, cfg.Rotation.DrainTimeout, int64(keystore.MaxRowsPerTx), cfg.Rotation.MaxPhaseALoops)
	defer func() {
		stopAutoRotate()
		autoCancel()
	}()

	proxyAddr := cfg.Server.BindAddress
	if bindHost, _, splitErr := net.SplitHostPort(proxyAddr); splitErr == nil && (bindHost == "0.0.0.0" || bindHost == "::") {
		slog.Warn("proxy bound to wildcard address; POST /v1/credentials responses will omit proxy_address field — clients must construct the URL from the external address",
			"bind_address", proxyAddr)
	}
	server := &http.Server{
		Addr:              proxyAddr,
		Handler:           proxyMux,
		ReadHeaderTimeout: cfg.Server.ReadHeaderTimeout,
	}
	adminServer2 := &http.Server{
		Addr:              cfg.Admin.Addr,
		Handler:           adminMux,
		ReadHeaderTimeout: cfg.Server.ReadHeaderTimeout,
	}

	errCh := make(chan error, 2)
	go func() {
		errCh <- server.ListenAndServe()
	}()
	go func() {
		errCh <- adminServer2.ListenAndServe()
	}()
	slog.Info("credential router listening",
		"proxy_addr", proxyAddr,
		"admin_addr", cfg.Admin.Addr,
		"version", version)

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt)
	if runtime.GOOS != "windows" {
		signal.Notify(sigCh, syscall.SIGTERM)
	}

	select {
	case sig := <-sigCh:
		slog.Info("shutdown signal received", "signal", sig.String())
		ctx, cancel := context.WithTimeout(context.Background(), cfg.Server.ShutdownTimeout)
		defer cancel()
		if err := server.Shutdown(ctx); err != nil {
			slog.Error("graceful shutdown failed", "error", err)
		}
		if err := adminServer2.Shutdown(ctx); err != nil {
			slog.Error("admin graceful shutdown failed", "error", err)
		}
		return 0
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("server stopped", "error", err)
			return 1
		}
		return 0
	}
}

func bootstrapKeystore(cfg platform.Config, st *store.Store, bm *backup.BackupManager) (*keystore.RecoveryResult, *keystore.Manager, error) {
	recoveryTimeout := cfg.Recovery.MaxWait
	if recoveryTimeout <= 0 {
		recoveryTimeout = platform.DefaultRecoveryMaxWait
	}
	ctx, cancel := context.WithTimeout(context.Background(), recoveryTimeout)
	defer cancel()

	res, err := keystore.RecoverFromState(ctx, cfg.SecretsDir(), st)
	if err != nil {
		return nil, nil, fmt.Errorf("recovery: %w", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		slog.Info("recovery actions taken", "case", res.Case, "actions", res.Actions)
	}

	mgr, err := keystore.LoadFromDir(ctx, cfg.SecretsDir(), cfg.DataDir, st)
	if err != nil {
		if isEmptyKeyMetadataErr(err) || isNotInitializedErr(err) {
			slog.Info("no key_metadata — running self-init")
			mgr, err = keystore.SelfInit(ctx, keystore.SelfInitParams{
				SecretsDir: cfg.SecretsDir(),
				CryptoMode: cfg.CryptoMode,
			}, st)
			if err != nil {
				return nil, nil, fmt.Errorf("self-init: %w", err)
			}
		} else {
			return nil, nil, fmt.Errorf("startup: %w", err)
		}
	}

	if err := keystore.StartupSyncConvergence(ctx, mgr, st); err != nil {
		return nil, nil, fmt.Errorf("sync convergence: %w", err)
	}
	if err := bm.ScanRetention(); err != nil {
		return nil, nil, fmt.Errorf("startup retention scan: %w", err)
	}
	return res, mgr, nil
}

func isEmptyKeyMetadataErr(err error) bool {
	return err != nil && errors.Is(err, store.ErrKeyMetadataEmpty)
}

func isNotInitializedErr(err error) bool {
	return err != nil && errors.Is(err, keystore.ErrNotInitialized)
}
