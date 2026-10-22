package metrics

import "github.com/prometheus/client_golang/prometheus"

var (
	TunnelDropTotal = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "gough_discovery_agent_tunnel_drop_total",
		Help: "Number of times the gRPC tunnel to api-manager dropped.",
	})
	BMCCertMismatchTotal = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "gough_bmc_certificate_mismatch_detected_total",
		Help: "Number of BMC certificate thumbprint mismatches detected.",
	})
	SmartFailureTotal = prometheus.NewCounter(prometheus.CounterOpts{
		Name: "gough_discovery_smart_failure_detected_total",
		Help: "Number of disks reporting SMART failure during discovery.",
	})
)

func Register() {
	prometheus.MustRegister(TunnelDropTotal, BMCCertMismatchTotal, SmartFailureTotal)
}
