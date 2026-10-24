//go:build windows

package tests_test

// Cross-platform test subprocess helpers for Windows. The Unix variant
// lives in proc_unix.go; both files expose the same five functions so
// callers can stay platform-agnostic.
//
// Windows semantics differ in two important ways:
//   - SysProcAttr.Setpgid / Pdeathsig do not exist. CREATE_NEW_PROCESS_GROUP
//     (the closest equivalent) makes the child the root of a new process
//     group; there is no parent-death-signal, so the test must explicitly
//     kill children via stopRouterProcess.
//   - There is no "kill the whole process group" primitive. Process.Kill
//     becomes TerminateProcess, which is single-process only. Grandchildren
//     spawned by the router must either shut themselves down on its exit
//     or be tolerated as test residue.

import (
	"os"
	"os/exec"
	"syscall"
)

// procGroupAttrs returns the SysProcAttr that creates a new process group
// for the child via CREATE_NEW_PROCESS_GROUP (Windows' closest analog to
// Setpgid). No Pdeathsig equivalent exists; the test is responsible for
// stopping its children via stopRouterProcess before exiting.
func procGroupAttrs() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP,
	}
}

// signalProcGroup delivers sig to the leader process only — Windows has
// no group-signal primitive. We pass sig straight through to Process.Signal,
// which on Windows only accepts os.Interrupt and os.Kill per the Go docs.
func signalProcGroup(p *os.Process, sig os.Signal) error {
	if p == nil {
		return nil
	}
	return p.Signal(sig)
}

// killProcGroup terminates the leader process via TerminateProcess. On
// Windows this is the strongest cross-process kill available; any process
// group semantics are dropped.
func killProcGroup(p *os.Process) error {
	if p == nil {
		return nil
	}
	return p.Kill()
}

// alive reports whether cmd's process is still running. Windows'
// Process.Signal only accepts os.Interrupt and os.Kill, so the null-signal
// probe used on Unix is unavailable. We track the state via ProcessState,
// which is set by the first Wait call and remains non-nil thereafter.
func alive(cmd *exec.Cmd) bool {
	if cmd == nil || cmd.Process == nil {
		return false
	}
	return cmd.ProcessState == nil
}

// interruptSignal returns the signal used as a graceful-shutdown request
// during stopRouterProcess. os.Interrupt is the only signal Windows
// Process.Signal is required to honor; the router's signal handler turns
// it into the same graceful-shutdown path as Unix SIGINT.
func interruptSignal() os.Signal {
	return os.Interrupt
}