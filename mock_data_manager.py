# mock_data_manager.py — run locally during Priority 0 test only
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockRegimeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "/analysis/regime" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {
                "pair": "BTCUSDT",
                "metric": "regime",
                "data": {
                    "regime": "high_volatility",
                    "regime_confidence": "high",
                    "volatility_level": "high",
                    "primary_signal": "volatility_percentile",
                    "thought_trace": "mock override for Priority 0 integration test",
                },
                "metadata": {
                    "timestamp": "2026-03-10T22:45:00.000000",
                    "collection": "mock_regime",
                },
            }
            self.wfile.write(json.dumps(payload).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[MOCK] {format % args}")


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8081), MockRegimeHandler)
    print("[MOCK] Data Manager running on port 8081. CTRL+C to stop.")
    server.serve_forever()
