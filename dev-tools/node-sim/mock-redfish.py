#!/usr/bin/env python3
"""mock-redfish.py — Minimal mock Redfish HTTP server for QEMU BMC simulation."""

import json
import argparse
from pathlib import Path
import http.server
import socketserver
from datetime import datetime

class RedfishHandler(http.server.BaseHTTPRequestHandler):
    """Minimal Redfish API handler."""

    def do_GET(self):
        """Handle GET requests (Redfish read operations)."""
        if self.path == "/redfish/v1/":
            self._json_response(200, {"@odata.id": "/redfish/v1/", "Systems": {"@odata.id": "/redfish/v1/Systems/"}})
        elif self.path == "/redfish/v1/Systems/":
            self._json_response(200, {
                "@odata.id": "/redfish/v1/Systems/",
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/"}]
            })
        elif self.path == "/redfish/v1/Systems/1/":
            self._json_response(200, {
                "@odata.id": "/redfish/v1/Systems/1/",
                "Id": "1",
                "Name": "Gough Sim Node",
                "SystemType": "Virtual",
                "Manufacturer": "QEMU",
                "Model": "gough-sim",
                "SerialNumber": self.server.dmi_uuid[:8],
                "UUID": self.server.dmi_uuid,
                "PowerState": "On",
                "Status": {"State": "Enabled", "Health": "OK"},
                "Oem": {
                    "SmartData": self.server.smart_data
                }
            })
        elif self.path == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset":
            self._json_response(405, {"error": "Use POST to reset"})
        else:
            self._json_response(404, {"error": "Not found"})

    def do_POST(self):
        """Handle POST requests (Redfish actions)."""
        if self.path == "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                reset_type = data.get("ResetType", "On")
                self._json_response(200, {
                    "TaskMonitor": "/redfish/v1/TaskService/Tasks/1",
                    "ResetType": reset_type
                })
            except json.JSONDecodeError:
                self._json_response(400, {"error": "Invalid JSON"})
        else:
            self._json_response(404, {"error": "Not found"})

    def _json_response(self, status_code, data):
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

def main():
    parser = argparse.ArgumentParser(description="Mock Redfish HTTP server")
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Load SMART data
    smart_file = args.output / "smartctl.json"
    smart_data = {}
    if smart_file.exists():
        with open(smart_file) as f:
            smart_data = json.load(f)

    # Custom handler with shared state
    class CustomRedfishHandler(RedfishHandler):
        pass

    # Create server with custom state
    with socketserver.TCPServer(("127.0.0.1", args.port), CustomRedfishHandler) as httpd:
        httpd.dmi_uuid = args.uuid
        httpd.smart_data = smart_data
        print(f"[mock-redfish] Serving on http://127.0.0.1:{args.port}/")
        print(f"[mock-redfish] DMI UUID: {args.uuid}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[mock-redfish] Shutting down")

if __name__ == "__main__":
    main()
