package platform_test

import (
	"errors"
	"fmt"
	"testing"

	"credential_router/internal/platform"
)

func TestHTTPStatusMapping(t *testing.T) {
	tests := []struct {
		code platform.Code
		want int
	}{
		{platform.CodeBadRequest, 400},
		{platform.CodeUnauthorized, 401},
		{platform.CodeNotFound, 404},
		{platform.CodeConflict, 409},
		{platform.CodeInternal, 500},
		{platform.CodeServiceUnavailable, 503},
		{platform.CodeRotationInProgress, 409},
		{platform.CodeNestedRotation, 409},
		{platform.CodeNoRotationInProgress, 409},
		{platform.CodeBackupFailed, 500},
		{platform.CodeUnsupportedCipherFormat, 400},
	}
	for _, tt := range tests {
		got := tt.code.HTTPStatus()
		if got != tt.want {
			t.Errorf("Code(%q).HTTPStatus() = %d, want %d", tt.code, got, tt.want)
		}
	}
}

func TestHTTPStatusDefault(t *testing.T) {
	if got := platform.Code("unknown").HTTPStatus(); got != 500 {
		t.Errorf("Code(unknown).HTTPStatus() = %d, want 500", got)
	}
}

func TestNew(t *testing.T) {
	e := platform.New(platform.CodeNotFound, "lookup", "credential not found")
	if e.Code != platform.CodeNotFound {
		t.Errorf("Code = %q, want %q", e.Code, platform.CodeNotFound)
	}
	if e.Op != "lookup" {
		t.Errorf("Op = %q, want %q", e.Op, "lookup")
	}
	if e.Message != "credential not found" {
		t.Errorf("Message = %q, want %q", e.Message, "credential not found")
	}
	if e.Cause != nil {
		t.Errorf("Cause = %v, want nil", e.Cause)
	}
}

func TestWrap(t *testing.T) {
	cause := errors.New("disk full")
	e := platform.Wrap(platform.CodeInternal, "backup", "backup failed", cause)
	if e.Code != platform.CodeInternal {
		t.Errorf("Code = %q, want %q", e.Code, platform.CodeInternal)
	}
	if e.Cause != cause {
		t.Errorf("Cause = %v, want %v", e.Cause, cause)
	}
	if !errors.Is(e, cause) {
		t.Errorf("errors.Is(e, cause) = false, want true — Unwrap not working")
	}
}

func TestErrorString(t *testing.T) {
	tests := []struct {
		name string
		err  *platform.Error
		want string
	}{
		{"op+msg", platform.New(platform.CodeBadRequest, "validate", "invalid input"), "validate: invalid input"},
		{"op+msg+cause", platform.Wrap(platform.CodeInternal, "db", "query failed", errors.New("connection refused")), "db: query failed: connection refused"},
		{"msg only", platform.New(platform.CodeNotFound, "", "missing"), "missing"},
		{"msg+cause", platform.Wrap(platform.CodeInternal, "", "fail", errors.New("reason")), "fail: reason"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.err.Error(); got != tt.want {
				t.Errorf("Error() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestCodeOf(t *testing.T) {
	e := platform.New(platform.CodeConflict, "store", "conflict")
	if got := platform.CodeOf(e); got != platform.CodeConflict {
		t.Errorf("CodeOf() = %q, want %q", got, platform.CodeConflict)
	}
}

func TestCodeOfNil(t *testing.T) {
	if got := platform.CodeOf(nil); got != platform.Code("") {
		t.Errorf("CodeOf(nil) = %q, want empty", got)
	}
}

func TestCodeOfPlainError(t *testing.T) {
	if got := platform.CodeOf(errors.New("plain error")); got != platform.Code("") {
		t.Errorf("CodeOf(plain) = %q, want empty", got)
	}
}

func TestCodeOfWrapped(t *testing.T) {
	inner := platform.New(platform.CodeNotFound, "inner", "not found")
	outer := fmt.Errorf("outer: %w", inner)
	if got := platform.CodeOf(outer); got != platform.CodeNotFound {
		t.Errorf("CodeOf(wrapped) = %q, want %q", got, platform.CodeNotFound)
	}
}

func TestCodeOfNestedErrs(t *testing.T) {
	// platform.Error wrapping another platform.Error
	inner := platform.New(platform.CodeBadRequest, "parse", "bad format")
	outer := platform.Wrap(platform.CodeInternal, "handler", "wrapped", inner)
	if got := platform.CodeOf(outer); got != platform.CodeInternal {
		t.Errorf("CodeOf(outer) = %q, want %q", got, platform.CodeInternal)
	}
}

func TestCodeOfNestedCause(t *testing.T) {
	// Access inner code via Cause chain
	inner := platform.New(platform.CodeNotFound, "repo", "not found")
	outer := platform.Wrap(platform.CodeInternal, "handler", "outer fail", inner)

	// CodeOf should return the outermost platform.Error's code
	if got := platform.CodeOf(outer); got != platform.CodeInternal {
		t.Errorf("CodeOf(outer) = %q, want %q", got, platform.CodeInternal)
	}

	// But errors.Is can still reach the inner through Unwrap
	if !errors.Is(outer, inner) {
		t.Errorf("errors.Is(outer, inner) = false, want true")
	}
}

func TestSentinelErrConflict(t *testing.T) {
	if platform.ErrConflict == nil {
		t.Fatal("ErrConflict is nil")
	}
	err := platform.Wrap(platform.CodeConflict, "test", "conflict occurred", platform.ErrConflict)
	if !errors.Is(err, platform.ErrConflict) {
		t.Errorf("errors.Is(Wrap(ErrConflict), ErrConflict) = false, want true")
	}
}

func TestSentinelErrNotFound(t *testing.T) {
	if platform.ErrNotFound == nil {
		t.Fatal("ErrNotFound is nil")
	}
	err := platform.Wrap(platform.CodeNotFound, "test", "not found", platform.ErrNotFound)
	if !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("errors.Is(Wrap(ErrNotFound), ErrNotFound) = false, want true")
	}
}

func TestSentinelErrBadRequest(t *testing.T) {
	if platform.ErrBadRequest == nil {
		t.Fatal("ErrBadRequest is nil")
	}
	err := platform.Wrap(platform.CodeBadRequest, "test", "bad request", platform.ErrBadRequest)
	if !errors.Is(err, platform.ErrBadRequest) {
		t.Errorf("errors.Is(Wrap(ErrBadRequest), ErrBadRequest) = false, want true")
	}
}

func TestSentinelDirectIs(t *testing.T) {
	if !errors.Is(platform.ErrConflict, platform.ErrConflict) {
		t.Errorf("errors.Is(ErrConflict, ErrConflict) = false, want true")
	}
	if !errors.Is(platform.ErrNotFound, platform.ErrNotFound) {
		t.Errorf("errors.Is(ErrNotFound, ErrNotFound) = false, want true")
	}
	if !errors.Is(platform.ErrBadRequest, platform.ErrBadRequest) {
		t.Errorf("errors.Is(ErrBadRequest, ErrBadRequest) = false, want true")
	}
}

func TestZeroValueError(t *testing.T) {
	var e platform.Error
	if e.Code != "" {
		t.Errorf("zero value Code = %q, want empty", e.Code)
	}
	if e.Error() != "" {
		t.Errorf("zero value Error() = %q, want empty", e.Error())
	}
}

func TestCodeOfEmptyCode(t *testing.T) {
	// An platform.Error with empty Code should still be detectable as platform.Error
	e := platform.New(platform.Code(""), "op", "msg")
	if got := platform.CodeOf(e); got != platform.Code("") {
		t.Errorf("CodeOf() = %q, want empty", got)
	}
}
