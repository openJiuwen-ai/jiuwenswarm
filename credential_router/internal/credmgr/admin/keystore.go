package admin

import (
	"context"
	"errors"
	"net/http"
	"time"

	"credential_router/internal/platform"
	"credential_router/internal/credmgr/keystore"
)

type healthResponse struct {
	Status            string `json:"status"`
	Keystore          string `json:"keystore"`
	Manager           bool   `json:"manager_ready"`
	ConvergenceState  string `json:"convergence_state,omitempty"`
	ConvergenceError  string `json:"convergence_error,omitempty"`
	ConvergenceStart  int64  `json:"convergence_started_at,omitempty"`
	ConvergenceFinish int64  `json:"convergence_finished_at,omitempty"`
	BuildInfo         string `json:"build_info,omitempty"`
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	km, err := s.store.GetKeyMetadata(ctx)
	if err != nil {
		respondJSON(w, http.StatusServiceUnavailable, healthResponse{
			Status:   "unavailable",
			Keystore: "missing",
		})
		return
	}

	status := "ok"
	if km.PendingKekVersion > 0 || km.PendingDekVersion > 0 {
		status = "rotating"
	}

	resp := healthResponse{
		Status:   status,
		Keystore: km.CryptoMode.String(),
		Manager:  s.rotator != nil,
	}
	if s.rotator != nil {
		cs := s.rotator.ConvergenceState()
		resp.ConvergenceState = cs.State
		resp.ConvergenceError = cs.Err
		if !cs.StartedAt.IsZero() {
			resp.ConvergenceStart = cs.StartedAt.Unix()
		}
		if !cs.FinishedAt.IsZero() {
			resp.ConvergenceFinish = cs.FinishedAt.Unix()
		}
		if cs.State == "failed" {
			resp.Status = "degraded"
			respondJSON(w, http.StatusServiceUnavailable, resp)
			return
		}
	}

	respondJSON(w, http.StatusOK, resp)
}

type keystoreStatusResponse struct {
	CryptoMode         string `json:"crypto_mode"`
	ActiveKekVersion   int64  `json:"active_kek_version"`
	PendingKekVersion  int64  `json:"pending_kek_version"`
	ActiveDekVersion   int64  `json:"active_dek_version"`
	PendingDekVersion  int64  `json:"pending_dek_version"`
	WrappedDEKPresent  bool   `json:"wrapped_dek_present"`
	FileShardVersion   int64  `json:"file_shard_version"`
	FileShardRotatedAt int64  `json:"file_shard_rotated_at"`
	DekRotatedAt       int64  `json:"dek_rotated_at"`
	LastRotateAt       int64  `json:"last_rotate_at"`
	UpdatedAt          int64  `json:"updated_at"`
	RotationState      string `json:"rotation_state"`
	StragglerCount     *int64 `json:"straggler_count"`
}

func (s *Server) keystoreStatus(w http.ResponseWriter, r *http.Request) {
	km, err := s.store.GetKeyMetadata(r.Context())
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			respondError(w, platform.Wrap(platform.CodeNotFound, "KeystoreStatus", "no key metadata", err))
			return
		}
		respondError(w, err)
		return
	}

	state := "idle"
	var straggler *int64
	switch {
	case km.PendingKekVersion > 0:
		state = "swap_pending"
	case km.PendingDekVersion > 0:
		state = "reencrypting"
		n, cErr := s.store.CountStragglersByDekVersion(r.Context(), km.ActiveDekVersion+1)
		if cErr != nil {
			respondError(w, cErr)
			return
		}
		straggler = &n
		if n == 0 {
			state = "ready_to_commit"
		}
	}

	respondJSON(w, http.StatusOK, keystoreStatusResponse{
		CryptoMode:         km.CryptoMode.String(),
		ActiveKekVersion:   km.ActiveKekVersion,
		PendingKekVersion:  km.PendingKekVersion,
		ActiveDekVersion:   km.ActiveDekVersion,
		PendingDekVersion:  km.PendingDekVersion,
		WrappedDEKPresent:  len(km.WrappedDEK) > 0,
		FileShardVersion:   km.FileShardVersion,
		FileShardRotatedAt: km.FileShardRotatedAt,
		DekRotatedAt:       km.DekRotatedAt,
		LastRotateAt:       km.LastRotateAt,
		UpdatedAt:          km.UpdatedAt,
		RotationState:      state,
		StragglerCount:     straggler,
	})
}

type shardRequest struct {
	Action string `json:"action"`
	S2     string `json:"s2,omitempty"`
}

func (s *Server) shardRotate(w http.ResponseWriter, r *http.Request) {
	var req shardRequest
	if r.ContentLength > 0 {
		if err := decodeJSON(r, &req); err != nil {
			respondError(w, err)
			return
		}
	}

	// Rotation lifecycle runs to completion (or recovery.max_wait) regardless of
	// client disconnect — derives from Background so r.Context() cancel doesn't
	// abort an in-progress rotation and leave the DB in pending state.
	beginCtx, cancel := context.WithTimeout(context.Background(), s.recoveryCfg.MaxWait)
	defer cancel()

	var err error
	switch req.Action {
	case "rotate-s1":
		err = s.rotator.BeginKEKRotation(beginCtx)
	case "rotate-s2":
		err = s.rotator.BeginS2Rotation(beginCtx, req.S2)
	default:
		respondError(w, platform.New(platform.CodeBadRequest, "ShardRotate", "action must be 'rotate-s1' or 'rotate-s2'"))
		return
	}

	if err != nil {
		if errors.Is(err, keystore.ErrRotationInProgress) {
			respondError(w, platform.Wrap(platform.CodeRotationInProgress, "ShardRotate", "rotation in progress", err))
			return
		}
		if errors.Is(err, keystore.ErrInvalidNewS1) {
			respondError(w, platform.Wrap(platform.CodeBadRequest, "ShardRotate", "invalid S2 or S1", err))
			return
		}
		respondError(w, err)
		return
	}

	// Synchronous Phase B: runs inline before the HTTP response is sent.
	// There is no background goroutine and no polling endpoint — if the
	// request returns 200, the rotation is committed; if it returns 5xx,
	// the caller retries the same admin request.
	if err := s.rotator.CompleteKEKRotation(beginCtx, s.rotationCfg.CompleteDrainTimeout); err != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "ShardRotate", "complete failed", err))
		return
	}

	km, kerr := s.store.GetKeyMetadata(beginCtx)
	if kerr != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "ShardRotate", "post-rotation metadata read", kerr))
		return
	}
	respondJSON(w, http.StatusOK, map[string]int64{"new_kek_version": km.ActiveKekVersion})
}

func (s *Server) dekRotate(w http.ResponseWriter, r *http.Request) {
	// Rotation lifecycle runs to completion (or recovery.max_wait) regardless of
	// client disconnect — derives from Background so r.Context() cancel doesn't
	// abort an in-progress rotation and leave the DB in pending state.
	ctx, cancel := context.WithTimeout(context.Background(), s.recoveryCfg.MaxWait)
	defer cancel()

	if err := s.rotator.BeginDEKRotation(ctx); err != nil {
		if errors.Is(err, keystore.ErrRotationInProgress) {
			respondError(w, platform.Wrap(platform.CodeRotationInProgress, "DEKRotate", "rotation in progress", err))
			return
		}
		respondError(w, err)
		return
	}
	if err := s.rotator.CompleteDEKRotation(ctx, s.rotationCfg.CompleteDrainTimeout, int64(keystore.MaxRowsPerTx), s.rotationCfg.MaxPhaseALoops); err != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "DEKRotate", "complete failed", err))
		return
	}
	km, kerr := s.store.GetKeyMetadata(ctx)
	if kerr != nil {
		respondError(w, platform.Wrap(platform.CodeInternal, "DEKRotate", "post-rotation metadata read", kerr))
		return
	}
	respondJSON(w, http.StatusOK, map[string]int64{"new_dek_version": km.ActiveDekVersion})
}
