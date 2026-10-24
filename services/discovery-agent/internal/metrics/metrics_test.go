//go:build noxdp

package metrics

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// counterValue returns the current value of the named counter from the default gatherer.
func counterValue(t *testing.T, name string) (float64, bool) {
	t.Helper()
	families, err := prometheus.DefaultGatherer.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		if len(f.GetMetric()) == 0 {
			return 0, true
		}
		return f.GetMetric()[0].GetCounter().GetValue(), true
	}
	return 0, false
}

// TestRegister verifies the three agent counters are exposed under their documented
// names on the default registry and that Inc is observable through Gather.
func TestRegister(t *testing.T) {
	Register()

	tests := []struct {
		name    string
		counter prometheus.Counter
		metric  string
	}{
		{name: "tunnel drop", counter: TunnelDropTotal, metric: "gough_discovery_agent_tunnel_drop_total"},
		{name: "bmc cert mismatch", counter: BMCCertMismatchTotal, metric: "gough_bmc_certificate_mismatch_detected_total"},
		{name: "smart failure", counter: SmartFailureTotal, metric: "gough_discovery_smart_failure_detected_total"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			before, ok := counterValue(t, tc.metric)
			if !ok {
				t.Fatalf("metric %q not exposed on default registry", tc.metric)
			}
			tc.counter.Inc()
			after, _ := counterValue(t, tc.metric)
			if after-before != 1 {
				t.Errorf("counter delta = %v, want 1", after-before)
			}
		})
	}
}
