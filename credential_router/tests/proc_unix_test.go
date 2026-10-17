//go:build !windows

package tests_test

// Cross-platform test subprocess helpers for the Unix family (Linux, macOS,
// FreeBSD, …). The Windows variant lives in proc_windows.go; both files
// expose the same five functions so callers can stay platform-agnostic.
//
// Why the helpers exist: the previous inlined code referenced
// syscall.SysProcAttr.Setpgid / Pdeathsig and syscall.Kill with negative
// pids — all of which fail to compile or fail at runtime on Windows. By
// routing every process-group operation through these helpers, the actual
// test bodies compile on every supported platform without per-platform
// build tags inside the test files themselves.

import (
	"os"
	"os/exec"
	"syscall"
)

// procGroupAttrs returns the SysProcAttr that puts the child in its own
// process group and arranges for the child to be SIGKILL'd if the test
// binary itself dies (covers the case where the test process is SIGKILL'd
// and would otherwise leak the spawned router).
func procGroupAttrs() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		Setpgid:   true,
		Pdeathsig: syscall.SIGKILL,
	}
}

// signalProcGroup delivers sig to the entire process group rooted at p.
// On Unix this is `kill(-pid, sig)`, so any grandchildren the router may
// have spawned also receive the signal.
func signalProcGroup(p *os.Process, sig os.Signal) error {
	if p == nil {
		return nil
	}
	if s, ok := sig.(syscall.Signal); ok {
		return syscall.Kill(-p.Pid, s)
	}
	return p.Signal(sig)
}

// killProcGroup unconditionally SIGKILLs the entire process group rooted
// at p. Used by stopRouterProcess as the hard-kill fallback after the
// graceful SIGINT times out.
func killProcGroup(p *os.Process) error {
	if p == nil {
		return nil
	}
	return syscall.Kill(-p.Pid, syscall.SIGKILL)
}

// alive reports whether cmd's process is still running. Implemented via
// the null-signal probe (kill -0) which is the canonical Unix way to
// distinguish "process exists" from "process already reaped".
func alive(cmd *exec.Cmd) bool {
	if cmd == nil || cmd.Process == nil {
		return false
	}
	return cmd.Process.Signal(syscall.Signal(0)) == nil
}

// interruptSignal returns the signal used as a graceful-shutdown request
// during stopRouterProcess. SIGINT gives the router a chance to flush the
// pidfile and release the flock before the hard kill lands.
func interruptSignal() os.Signal {
	return syscall.SIGINT
}