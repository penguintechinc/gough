//go:build noxdp

package main

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
)

// fixedRand returns a constant value in [0,1) for deterministic jitter.
func fixedRand(v float64) func() float64 { return func() float64 { return v } }

// TestBackoffDelay checks exponential growth, the cap, and jitter bounds.
func TestBackoffDelay(t *testing.T) {
	p := retryPolicy{Initial: time.Second, Max: 8 * time.Second, Multiplier: 2, Jitter: 0.2}

	tests := []struct {
		name    string
		attempt int
		rnd     float64
		want    time.Duration
	}{
		{name: "attempt 0 no jitter", attempt: 0, rnd: 0.5, want: time.Second},
		{name: "attempt 1 doubles", attempt: 1, rnd: 0.5, want: 2 * time.Second},
		{name: "attempt 2 doubles", attempt: 2, rnd: 0.5, want: 4 * time.Second},
		{name: "attempt 3 hits cap", attempt: 3, rnd: 0.5, want: 8 * time.Second},
		{name: "attempt 40 stays capped", attempt: 40, rnd: 0.5, want: 8 * time.Second},
		{name: "low jitter -20%", attempt: 0, rnd: 0.0, want: 800 * time.Millisecond},
		{name: "high jitter +20%", attempt: 0, rnd: 0.999999, want: 1200 * time.Millisecond},
		{name: "jitter clamped at cap", attempt: 3, rnd: 0.999999, want: 8 * time.Second},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := backoffDelay(tc.attempt, p, fixedRand(tc.rnd))
			// Allow 1ms slack for float rounding on the jittered rows.
			if d := got - tc.want; d > time.Millisecond || d < -time.Millisecond {
				t.Errorf("backoffDelay(%d) = %v, want %v", tc.attempt, got, tc.want)
			}
		})
	}
}

// TestBackoffDelayZeroJitter confirms a zero-jitter policy is exactly deterministic.
func TestBackoffDelayZeroJitter(t *testing.T) {
	p := retryPolicy{Initial: 100 * time.Millisecond, Max: time.Second, Multiplier: 3, Jitter: 0}
	tests := []struct {
		attempt int
		want    time.Duration
	}{
		{attempt: 0, want: 100 * time.Millisecond},
		{attempt: 1, want: 300 * time.Millisecond},
		{attempt: 2, want: 900 * time.Millisecond},
		{attempt: 3, want: time.Second},
	}
	for _, tc := range tests {
		if got := backoffDelay(tc.attempt, p, fixedRand(0.37)); got != tc.want {
			t.Errorf("attempt %d = %v, want %v", tc.attempt, got, tc.want)
		}
	}
}

// TestSleepCtx covers normal expiry and cancellation.
func TestSleepCtx(t *testing.T) {
	tests := []struct {
		name    string
		ctx     func() (context.Context, context.CancelFunc)
		d       time.Duration
		wantErr error
	}{
		{
			name: "completes",
			ctx:  func() (context.Context, context.CancelFunc) { return context.WithCancel(context.Background()) },
			d:    time.Millisecond,
		},
		{
			name: "cancelled during sleep",
			ctx: func() (context.Context, context.CancelFunc) {
				c, cancel := context.WithCancel(context.Background())
				cancel()
				return c, cancel
			},
			d:       time.Hour,
			wantErr: context.Canceled,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := tc.ctx()
			defer cancel()
			if err := sleepCtx(ctx, tc.d); !errors.Is(err, tc.wantErr) {
				t.Errorf("sleepCtx error = %v, want %v", err, tc.wantErr)
			}
		})
	}
}

// scripted returns a post func that yields the given errors in order (nil = success).
func scripted(results ...error) (func(context.Context) (discover.Response, error), *int) {
	calls := 0
	return func(context.Context) (discover.Response, error) {
		i := calls
		calls++
		if i >= len(results) {
			return discover.Response{}, errors.New("script exhausted")
		}
		if results[i] != nil {
			return discover.Response{}, results[i]
		}
		return discover.Response{NodeID: "node-1"}, nil
	}, &calls
}

// TestPostWithRetry covers success, transient retries, permanent rejection, and cancellation.
func TestPostWithRetry(t *testing.T) {
	transient := &discover.StatusError{Code: http.StatusServiceUnavailable, URL: "u"}
	unauth := &discover.StatusError{Code: http.StatusUnauthorized, URL: "u"}
	forbid := &discover.StatusError{Code: http.StatusForbidden, URL: "u"}
	netErr := errors.New("dial tcp: connection refused")

	tests := []struct {
		name       string
		script     []error
		wantCalls  int
		wantSleeps int
		wantFails  int
		wantErr    bool
		wantPerm   bool
	}{
		{name: "first attempt succeeds", script: []error{nil}, wantCalls: 1},
		{name: "transient 503 then success", script: []error{transient, transient, nil}, wantCalls: 3, wantSleeps: 2, wantFails: 2},
		{name: "network error then success", script: []error{netErr, nil}, wantCalls: 2, wantSleeps: 1, wantFails: 1},
		{name: "401 is never retried", script: []error{unauth, nil}, wantCalls: 1, wantErr: true, wantPerm: true},
		{name: "403 is never retried", script: []error{forbid, nil}, wantCalls: 1, wantErr: true, wantPerm: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			post, calls := scripted(tc.script...)
			var sleeps, fails int
			sleep := func(context.Context, time.Duration) error { sleeps++; return nil }
			onFail := func(int, error) { fails++ }

			resp, err := postWithRetry(context.Background(), post, retryPolicy{Initial: time.Millisecond, Max: time.Millisecond, Multiplier: 2}, fixedRand(0.5), sleep, onFail)

			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr %v", err, tc.wantErr)
			}
			if *calls != tc.wantCalls {
				t.Errorf("post calls = %d, want %d", *calls, tc.wantCalls)
			}
			if sleeps != tc.wantSleeps {
				t.Errorf("sleeps = %d, want %d", sleeps, tc.wantSleeps)
			}
			if fails != tc.wantFails {
				t.Errorf("onFail calls = %d, want %d", fails, tc.wantFails)
			}
			if got := discover.IsPermanent(err); got != tc.wantPerm {
				t.Errorf("IsPermanent(err) = %v, want %v", got, tc.wantPerm)
			}
			if !tc.wantErr && resp.NodeID != "node-1" {
				t.Errorf("resp.NodeID = %q, want node-1", resp.NodeID)
			}
		})
	}
}

// TestPostWithRetryDelaysGrow records the delays handed to sleep and checks they grow then cap.
func TestPostWithRetryDelaysGrow(t *testing.T) {
	transient := &discover.StatusError{Code: http.StatusBadGateway, URL: "u"}
	script := []error{transient, transient, transient, transient, transient, nil}
	post, _ := scripted(script...)

	var delays []time.Duration
	sleep := func(_ context.Context, d time.Duration) error {
		delays = append(delays, d)
		return nil
	}
	p := retryPolicy{Initial: time.Second, Max: 3 * time.Second, Multiplier: 2, Jitter: 0}

	if _, err := postWithRetry(context.Background(), post, p, fixedRand(0.5), sleep, nil); err != nil {
		t.Fatalf("postWithRetry: %v", err)
	}
	want := []time.Duration{time.Second, 2 * time.Second, 3 * time.Second, 3 * time.Second, 3 * time.Second}
	if len(delays) != len(want) {
		t.Fatalf("delays = %v, want %v", delays, want)
	}
	for i := range want {
		if delays[i] != want[i] {
			t.Errorf("delay[%d] = %v, want %v", i, delays[i], want[i])
		}
	}
}

// TestPostWithRetryContext covers cancellation before the first call and during backoff.
func TestPostWithRetryContext(t *testing.T) {
	transient := &discover.StatusError{Code: http.StatusInternalServerError, URL: "u"}

	t.Run("already cancelled never posts", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		post, calls := scripted(nil)
		_, err := postWithRetry(ctx, post, discoverRetry, fixedRand(0.5), sleepCtx, nil)
		if !errors.Is(err, context.Canceled) {
			t.Errorf("error = %v, want context.Canceled", err)
		}
		if *calls != 0 {
			t.Errorf("post calls = %d, want 0", *calls)
		}
	})

	t.Run("cancel during backoff aborts", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		post, _ := scripted(transient, transient, nil)
		sleep := func(c context.Context, _ time.Duration) error {
			cancel()
			return sleepCtx(c, time.Hour)
		}
		_, err := postWithRetry(ctx, post, discoverRetry, fixedRand(0.5), sleep, nil)
		if !errors.Is(err, context.Canceled) {
			t.Errorf("error = %v, want context.Canceled", err)
		}
	})

	t.Run("sleep error propagates", func(t *testing.T) {
		post, _ := scripted(transient, nil)
		boom := errors.New("sleep failed")
		_, err := postWithRetry(context.Background(), post, discoverRetry, fixedRand(0.5),
			func(context.Context, time.Duration) error { return boom }, nil)
		if !errors.Is(err, boom) {
			t.Errorf("error = %v, want sleep error", err)
		}
	})
}

// TestDiscoverRetryPolicy pins the production schedule so accidental edits are caught.
func TestDiscoverRetryPolicy(t *testing.T) {
	if discoverRetry.Initial != time.Second || discoverRetry.Max != 60*time.Second {
		t.Errorf("policy = %+v, want 1s initial / 60s max", discoverRetry)
	}
	if discoverRetry.Jitter <= 0 || discoverRetry.Jitter >= 1 {
		t.Errorf("jitter = %v, want (0,1)", discoverRetry.Jitter)
	}
}
