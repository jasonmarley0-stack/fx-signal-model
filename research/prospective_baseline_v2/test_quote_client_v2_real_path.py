"""Integration test for quote_client.py's REAL request path (request_fn
left at its default, i.e. _real_request_fn() -- an actual oandapyV20.API
client with actual request_params={"timeout": ...}), proven against a
genuinely slow LOCAL HTTP server. An injected fake request_fn that
"voluntarily" raises after some simulated delay proves nothing about
whether the real requests.Session() timeout is actually wired up -- this
test redirects oandapyV20's own practice-environment base URL at a local
socket that never responds, and asserts the call still returns promptly.

No real network, no real credentials (fake OANDA_API_KEY/OANDA_ACCOUNT_ID
env vars -- never sent anywhere but localhost).
"""
from __future__ import annotations
import http.server
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
import contract as cfg  # noqa: E402
from quote_client import fetch_pricing_samples  # noqa: E402


class _HangingHandler(http.server.BaseHTTPRequestHandler):
    """Accepts the connection, then never writes a response -- simulates
    exactly the class of failure (a request that never completes) the
    production timeout must bound."""

    def do_GET(self):  # noqa: N802
        time.sleep(30)  # far longer than any timeout this test configures

    def log_message(self, format, *args):  # noqa: A002 -- silence test output
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_request_path_enforces_an_actual_socket_timeout_against_a_hanging_server():
    """The actual production code path (request_fn=None -> _real_request_fn())
    against a local server that never responds. Must return in roughly
    REQUEST_TIMEOUT_SECONDS, not hang for 30s, proving the real
    requests.Session() timeout -- not just loop-level bookkeeping -- is
    genuinely wired up via oandapyV20's request_params mechanism."""
    import oandapyV20.oandapyV20 as oanda_module

    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _HangingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_env = oanda_module.TRADING_ENVIRONMENTS["practice"]["api"]
    original_timeout = cfg.REQUEST_TIMEOUT_SECONDS
    original_total = cfg.MAX_TOTAL_REQUEST_SECONDS
    original_retries = cfg.MAX_RETRY_ATTEMPTS
    original_backoff = cfg.RETRY_BACKOFF_BASE_SECONDS
    os.environ["OANDA_API_KEY"] = "test-fake-key-never-sent-to-real-oanda"
    os.environ["OANDA_ACCOUNT_ID"] = "test-fake-account"
    os.environ["OANDA_ENVIRONMENT"] = "practice"
    try:
        oanda_module.TRADING_ENVIRONMENTS["practice"]["api"] = f"http://127.0.0.1:{port}"
        # Fast-but-real bounds for this test -- same mechanism as production, just small numbers:
        cfg.REQUEST_TIMEOUT_SECONDS = 0.3
        cfg.MAX_TOTAL_REQUEST_SECONDS = 1.0
        cfg.MAX_RETRY_ATTEMPTS = 1
        cfg.RETRY_BACKOFF_BASE_SECONDS = 0.1

        start = time.monotonic()
        result = fetch_pricing_samples(["EURUSD"])  # request_fn=None -- the REAL path, not an injected fake
        elapsed = time.monotonic() - start

        assert result["samples"] == []
        assert len(result["attempts"]) >= 1
        assert all(a.get("outcome") == "failed" or a.get("skipped") for a in result["attempts"])
        assert elapsed < 5.0, (
            f"the real request path took {elapsed:.1f}s against a hanging server -- "
            f"the socket-level timeout is not actually bounding it")
        print(f"v2 quote_client (REAL path): a genuinely hanging local server is bounded in {elapsed:.2f}s "
              f"(not the server's 30s hang) — the real requests.Session() timeout is genuinely wired up: OK")
    finally:
        oanda_module.TRADING_ENVIRONMENTS["practice"]["api"] = original_env
        cfg.REQUEST_TIMEOUT_SECONDS = original_timeout
        cfg.MAX_TOTAL_REQUEST_SECONDS = original_total
        cfg.MAX_RETRY_ATTEMPTS = original_retries
        cfg.RETRY_BACKOFF_BASE_SECONDS = original_backoff
        del os.environ["OANDA_API_KEY"]
        del os.environ["OANDA_ACCOUNT_ID"]
        del os.environ["OANDA_ENVIRONMENT"]
        server.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    test_real_request_path_enforces_an_actual_socket_timeout_against_a_hanging_server()
    print("All v2 quote_client real-path tests passed.")
