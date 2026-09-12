// Package errs provides the central error envelope for credential_router.
//
// Every package in credential_router wraps its errors with an *Error carrying
// a machine-readable Code, the logical operation name, a human message, and
// an optional Cause. Callers can switch on CodeOf(err) for HTTP routing or
// use errors.Is for sentinel matching.
package platform

import (
	"errors"
	"strings"
)

// Code is a machine-readable error category.
type Code string

// HTTP status codes for each error code. Centralized so the rest of the
// codebase never hardcodes status numbers; new Code values fail loud here.
const (
	CodeBadRequest              Code = "bad_request"
	CodeUnauthorized            Code = "unauthorized"
	CodeNotFound                Code = "not_found"
	CodeConflict                Code = "conflict"
	CodeInternal                Code = "internal"
	CodeServiceUnavailable      Code = "service_unavailable"
	CodeRotationInProgress      Code = "rotation_in_progress"
	CodeNestedRotation          Code = "nested_rotation"
	CodeNoRotationInProgress    Code = "no_rotation_in_progress"
	CodeBackupFailed            Code = "backup_failed"
	CodeUnsupportedCipherFormat Code = "unsupported_cipher_format"
	CodePayloadTooLarge         Code = "payload_too_large"
	CodeBadGateway              Code = "bad_gateway"
	CodeGatewayTimeout          Code = "gateway_timeout"
)

// HTTPStatus returns the HTTP status code for the error code.
// Unknown codes default to 500.
func (c Code) HTTPStatus() int {
	switch c {
	case CodeBadRequest, CodeUnsupportedCipherFormat:
		return 400
	case CodeUnauthorized:
		return 401
	case CodeNotFound:
		return 404
	case CodeConflict, CodeRotationInProgress, CodeNestedRotation, CodeNoRotationInProgress:
		return 409
	case CodePayloadTooLarge:
		return 413
	case CodeBackupFailed, CodeInternal:
		return 500
	case CodeBadGateway:
		return 502
	case CodeServiceUnavailable:
		return 503
	case CodeGatewayTimeout:
		return 504
	default:
		return 500
	}
}

// Sentinel errors for use with errors.Is.
var (
	ErrConflict         = errors.New("conflict")
	ErrNotFound         = errors.New("not found")
	ErrBadRequest       = errors.New("bad request")
	ErrServiceUnavailable = errors.New("service unavailable")
	ErrPayloadTooLarge  = errors.New("payload too large")
	ErrBadGateway       = errors.New("bad gateway")
	ErrGatewayTimeout   = errors.New("gateway timeout")
)

// Error is the credential_router error envelope.
type Error struct {
	Code    Code   // machine-readable category
	Op      string // logical operation name (e.g. "CredentialStore.Get")
	Message string // human-readable description
	Cause   error  // underlying error (optional)
}

// New creates a new Error with the given code, operation, and message.
func New(code Code, op, msg string) *Error {
	return &Error{Code: code, Op: op, Message: msg}
}

// Wrap creates a new Error wrapping an existing error.
func Wrap(code Code, op, msg string, cause error) *Error {
	return &Error{Code: code, Op: op, Message: msg, Cause: cause}
}

func (e *Error) Error() string {
	var b strings.Builder
	if e.Op != "" {
		b.WriteString(e.Op)
		b.WriteString(": ")
	}
	b.WriteString(e.Message)
	if e.Cause != nil {
		b.WriteString(": ")
		b.WriteString(e.Cause.Error())
	}
	return b.String()
}

// Unwrap returns the Cause, enabling errors.Is and errors.As to walk the chain.
func (e *Error) Unwrap() error { return e.Cause }

// CodeOf extracts the Code from an error, unwrapping through the Cause chain.
// Returns the empty Code ("") if err is nil or not an *Error.
func CodeOf(err error) Code {
	if err == nil {
		return Code("")
	}
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	return Code("")
}
