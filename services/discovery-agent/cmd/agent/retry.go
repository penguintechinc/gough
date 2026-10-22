//go:build noxdp

package main

import (
	"context"
	"fmt"
	"time"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/discover"
)

// retryPolicy describes the reconnect schedule for the discover POST.
type retryPolicy struct {
	// Initial is the delay before the first retry.
	Initial time.Duration
	// Max caps any single delay.
	Max time.Duration
	// Multiplier grows the delay per attempt.
	Multiplier float64
	// Jitter is the +/- fraction applied to each delay (0.2 = +/-20%).
	Jitter float64
}

// discoverRetry is the production schedule: 1s, 2s, 4s ... capped at 60s, +/-20% jitter.
var discoverRetry = retryPolicy{
	Initial:    time.Second,
	Max:        60 * time.Second,
	Multiplier: 2.0,
	Jitter:     0.2,
}

// backoffDelay returns the delay before retry number attempt (0-based).
// rnd must return values in [0,1); it is injected so tests are deterministic.
func backoffDelay(attempt int, p retryPolicy, rnd func() float64) time.Duration {
	base := float64(p.Initial)
	limit := float64(p.Max)
	for i := 0; i < attempt && base < limit; i++ {
		base *= p.Multiplier
	}
	if base > limit {
		base = limit
	}
	// Map rnd() in [0,1) to a factor in [1-Jitter, 1+Jitter).
	factor := 1 + p.Jitter*(2*rnd()-1)
	d := base * factor
	if d > limit {
		d = limit
	}
	if d < 0 {
		d = 0
	}
	return time.Duration(d)
}

// sleepCtx waits for d or until ctx is done, whichever comes first.
func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// postWithRetry calls post until it succeeds, a permanent error occurs (401/403),
// or ctx is done. Transient failures are reported through onFail and retried
// after backoffDelay. sleep and rnd are injected for deterministic tests.
func postWithRetry(
	ctx context.Context,
	post func(context.Context) (discover.Response, error),
	p retryPolicy,
	rnd func() float64,
	sleep func(context.Context, time.Duration) error,
	onFail func(attempt int, err error),
) (discover.Response, error) {
	for attempt := 0; ; attempt++ {
		if err := ctx.Err(); err != nil {
			return discover.Response{}, err
		}
		resp, err := post(ctx)
		if err == nil {
			return resp, nil
		}
		if discover.IsPermanent(err) {
			return discover.Response{}, fmt.Errorf("bootstrap token rejected, not retrying: %w", err)
		}
		if onFail != nil {
			onFail(attempt, err)
		}
		if serr := sleep(ctx, backoffDelay(attempt, p, rnd)); serr != nil {
			return discover.Response{}, serr
		}
	}
}
