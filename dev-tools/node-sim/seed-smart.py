#!/usr/bin/env python3
"""seed-smart.py — Generate synthetic smartmontools fixtures per pattern."""

import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import random

SMART_HEALTHY = {
    "smartctl": {
        "version": [7, 3],
        "svn_revision": "5555",
        "smartctl_exit_status": 0,
        "argv": ["smartctl", "-a", "-json"],
        "exit_status": 0
    },
    "device": {
        "name": "/dev/sda",
        "info_name": "/dev/sda",
        "type": "sat",
        "protocol": "SATA"
    },
    "smart_status": {
        "passed": True
    },
    "ata_smart_data": {
        "offline_data_collection": {
            "status": {
                "value": 0,
                "string": "was never started"
            }
        }
    },
    "ata_smart_attributes": {
        "table": [
            {
                "number": 5,
                "name": "Reallocated_Sector_Ct",
                "value": 100,
                "worst": 100,
                "thresh": 10,
                "when_failed": "",
                "raw": {"value": 0, "string": "0"}
            },
            {
                "number": 9,
                "name": "Power_On_Hours",
                "value": 99,
                "worst": 99,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 1234, "string": "1234"}
            },
            {
                "number": 197,
                "name": "Current_Pending_Sector",
                "value": 100,
                "worst": 100,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 0, "string": "0"}
            },
            {
                "number": 199,
                "name": "UDMA_CRC_Error_Count",
                "value": 100,
                "worst": 100,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 0, "string": "0"}
            }
        ]
    }
}

SMART_WARNING = {
    "smartctl": SMART_HEALTHY["smartctl"].copy(),
    "device": SMART_HEALTHY["device"].copy(),
    "smart_status": {
        "passed": False
    },
    "ata_smart_data": SMART_HEALTHY["ata_smart_data"].copy(),
    "ata_smart_attributes": {
        "table": [
            {
                "number": 5,
                "name": "Reallocated_Sector_Ct",
                "value": 80,
                "worst": 80,
                "thresh": 10,
                "when_failed": "",
                "raw": {"value": 50, "string": "50 (threshold: 10)"}
            },
            {
                "number": 9,
                "name": "Power_On_Hours",
                "value": 99,
                "worst": 99,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 10234, "string": "10234"}
            },
            {
                "number": 197,
                "name": "Current_Pending_Sector",
                "value": 85,
                "worst": 85,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 10, "string": "10"}
            },
            {
                "number": 199,
                "name": "UDMA_CRC_Error_Count",
                "value": 100,
                "worst": 100,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 0, "string": "0"}
            }
        ]
    }
}

SMART_FAILING = {
    "smartctl": {**SMART_HEALTHY["smartctl"], "smartctl_exit_status": 4, "exit_status": 4},
    "device": SMART_HEALTHY["device"].copy(),
    "smart_status": {
        "passed": False
    },
    "ata_smart_data": SMART_HEALTHY["ata_smart_data"].copy(),
    "ata_smart_attributes": {
        "table": [
            {
                "number": 5,
                "name": "Reallocated_Sector_Ct",
                "value": 10,
                "worst": 10,
                "thresh": 10,
                "when_failed": "NOW",
                "raw": {"value": 500, "string": "500 FAILING"}
            },
            {
                "number": 9,
                "name": "Power_On_Hours",
                "value": 99,
                "worst": 99,
                "thresh": 0,
                "when_failed": "",
                "raw": {"value": 50000, "string": "50000"}
            },
            {
                "number": 197,
                "name": "Current_Pending_Sector",
                "value": 1,
                "worst": 1,
                "thresh": 0,
                "when_failed": "NOW",
                "raw": {"value": 1000, "string": "1000 FAILING"}
            },
            {
                "number": 199,
                "name": "UDMA_CRC_Error_Count",
                "value": 50,
                "worst": 50,
                "thresh": 0,
                "when_failed": "NOW",
                "raw": {"value": 9999, "string": "9999 FAILING"}
            }
        ]
    }
}

def main():
    parser = argparse.ArgumentParser(description="Generate smartmontools JSON fixtures")
    parser.add_argument("--pattern", choices=["healthy", "warning", "failing"], default="healthy")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    patterns = {
        "healthy": SMART_HEALTHY,
        "warning": SMART_WARNING,
        "failing": SMART_FAILING
    }

    fixture = patterns[args.pattern]
    fixture["smartctl"]["execution_time_ms"] = random.randint(100, 500)
    fixture["ata_smart_data"]["offline_data_collection"]["completion_time_min"] = \
        random.randint(0, 60)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(fixture, f, indent=2)

    print(f"[seed-smart] Generated {args.pattern} fixture: {args.output}")

if __name__ == "__main__":
    main()
