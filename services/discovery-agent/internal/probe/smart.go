//go:build noxdp

package probe

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/penguintechinc/gough/services/discovery-agent/internal/exec"
)

// SmartOutput is the parsed result of smartctl -a -j /dev/<dev>.
type SmartOutput struct {
	Device struct {
		Name     string `json:"name"`
		InfoName string `json:"info_name"`
		Type     string `json:"type"`
		Protocol string `json:"protocol"`
	} `json:"device"`
	ModelFamily string `json:"model_family"`
	ModelName   string `json:"model_name"`
	SerialNumber string `json:"serial_number"`
	FirmwareVersion string `json:"firmware_version"`
	UserCapacity struct {
		Blocks int64 `json:"blocks"`
		Bytes  int64 `json:"bytes"`
	} `json:"user_capacity"`
	SmartStatus struct {
		Passed bool `json:"passed"`
	} `json:"smart_status"`
	ATASmartAttributes struct {
		Table []SmartAttribute `json:"table"`
	} `json:"ata_smart_attributes"`
	NvmeSmartHealthInformation *NvmeSMARTHealth `json:"nvme_smart_health_information_log,omitempty"`
	PowerOnTime struct {
		Hours int `json:"hours"`
	} `json:"power_on_time"`
	PowerCycleCount int `json:"power_cycle_count"`
	Temperature struct {
		Current int `json:"current"`
	} `json:"temperature"`
	// DriveType is set by the caller based on transport type.
	DriveType string `json:"drive_type,omitempty"`
	// Raw is the original JSON for forwarding.
	Raw json.RawMessage `json:"-"`
}

// SmartAttribute represents a single ATA SMART attribute.
type SmartAttribute struct {
	ID         int    `json:"id"`
	Name       string `json:"name"`
	Value      int    `json:"value"`
	Worst      int    `json:"worst"`
	Thresh     int    `json:"thresh"`
	WhenFailed string `json:"when_failed"`
	Flags      struct {
		AutoKeep bool `json:"auto_keep"`
		ErrorRate bool `json:"error_rate"`
		EventCount bool `json:"event_count"`
		Performance bool `json:"performance"`
		Prefailure bool `json:"prefailure"`
		UpdatedOnline bool `json:"updated_online"`
		Value int `json:"value"`
	} `json:"flags"`
	Raw struct {
		Value int64  `json:"value"`
		String string `json:"string"`
	} `json:"raw"`
}

// NvmeSMARTHealth holds NVMe health log fields.
type NvmeSMARTHealth struct {
	CriticalWarning         int   `json:"critical_warning"`
	TemperatureCelsius      int   `json:"temperature"`
	AvailableSpare          int   `json:"available_spare"`
	AvailableSpareThreshold int   `json:"available_spare_threshold"`
	PercentageUsed          int   `json:"percentage_used"`
	DataUnitsRead           int64 `json:"data_units_read"`
	DataUnitsWritten        int64 `json:"data_units_written"`
	PowerCycles             int64 `json:"power_cycles"`
	PowerOnHours            int64 `json:"power_on_hours"`
	UnsafeShutdowns         int64 `json:"unsafe_shutdowns"`
	MediaErrors             int64 `json:"media_errors"`
	NumErrLogEntries        int64 `json:"num_err_log_entries"`
}

// RunSmart runs smartctl -a -j on a single device path (e.g. "/dev/sda").
func RunSmart(ctx context.Context, runner exec.Runner, devPath string) (SmartOutput, error) {
	out, err := runner.Run(ctx, "smartctl", "-a", "-j", devPath)
	// smartctl exits non-zero for warnings; we still parse what we got.
	if err != nil && len(out) == 0 {
		return SmartOutput{}, fmt.Errorf("smartctl %s: %w", devPath, err)
	}

	var result SmartOutput
	if parseErr := json.Unmarshal(out, &result); parseErr != nil {
		return SmartOutput{}, fmt.Errorf("smartctl %s parse: %w", devPath, parseErr)
	}
	result.Raw = out

	// Derive drive type from device protocol and type fields.
	proto := strings.ToLower(result.Device.Protocol)
	devType := strings.ToLower(result.Device.Type)
	switch {
	case strings.Contains(proto, "nvme") || strings.Contains(devType, "nvme"):
		result.DriveType = "nvme"
	case strings.Contains(proto, "sas") || strings.Contains(devType, "sas"):
		result.DriveType = "sas"
	case strings.Contains(proto, "scsi") || strings.Contains(devType, "scsi"):
		// SCSI bus can be SAS or SATA; examine device name hint for best-effort.
		// smartctl uses "scsi" type for SAS drives; fall back to "sas".
		result.DriveType = "sas"
	case strings.Contains(proto, "ata") || strings.Contains(devType, "sat"):
		result.DriveType = "sata"
	default:
		result.DriveType = "unknown"
	}

	return result, nil
}

// SmartStatusString converts the bool SmartStatus.Passed field to the
// canonical string form expected by the api-manager: "healthy" or "failed".
func SmartStatusString(passed bool) string {
	if passed {
		return "healthy"
	}
	return "failed"
}

// RunSmartAllDisks runs smartctl on every top-level disk from lsblk output.
// Failures per-device are collected; a partial result with errors is returned
// rather than aborting entirely.
func RunSmartAllDisks(ctx context.Context, runner exec.Runner, disks []BlockDevice) ([]SmartOutput, []error) {
	var results []SmartOutput
	var errs []error
	for _, d := range disks {
		path := "/dev/" + d.Name
		s, err := RunSmart(ctx, runner, path)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		results = append(results, s)
	}
	return results, errs
}
