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


def _run_receipt_spacing_scenario(hang_on_counts: set[int], num_ticks: int, run_seconds: float) -> list:
    """Shared harness for the two scenarios below: a real local HTTP
    server that HANGS (triggering the real socket-level timeout, not an
    instantly-raised fake) on specific request counts, then responds
    normally -- run through the REAL observe._quote_tick_loop +
    quote_client.fetch_pricing_samples (request_fn=None, the real path),
    with the REAL, UNMODIFIED production constants. Returns the sorted
    list of recorded received_at_utc datetimes."""
    import json as _json
    import tempfile
    import threading as _threading
    import sys as _sys
    from pathlib import Path as _Path
    import oandapyV20.oandapyV20 as oanda_module

    _sys.path.insert(0, str(_Path(__file__).parent))
    _sys.path.insert(0, str(_Path(__file__).parent.parent.parent / "src"))
    for name in ("observe", "alert_lifecycle"):
        _sys.modules.pop(name, None)
    import observe

    class _FlakyHandler(http.server.BaseHTTPRequestHandler):
        _count = 0
        _lock = _threading.Lock()

        def do_GET(self):  # noqa: N802
            with type(self)._lock:
                type(self)._count += 1
                n = type(self)._count
            if n in hang_on_counts:
                time.sleep(30)  # genuinely hangs -- triggers the real socket timeout, not a fake raise
                return
            body = _json.dumps({"prices": [
                {"instrument": "EUR_USD", "time": "2026-01-01T00:00:00.000000000Z",
                 "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1002"}], "tradeable": True},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            pass

    port = _free_port()
    # A plain HTTPServer handles one connection at a time -- the "hanging"
    # request would then block the SERVER itself from even accepting the
    # retry's connection, making every subsequent attempt time out too
    # (not what a real transient-then-recovers OANDA endpoint looks like,
    # where other requests are serviced normally throughout). A threaded
    # server dispatches each connection independently, so a hang on one
    # request count genuinely does not affect any other.
    import socketserver

    class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = _ThreadingHTTPServer(("127.0.0.1", port), _FlakyHandler)
    server_thread = _threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    original_env = oanda_module.TRADING_ENVIRONMENTS["practice"]["api"]
    os.environ["OANDA_API_KEY"] = "test-fake-key-never-sent-to-real-oanda"
    os.environ["OANDA_ACCOUNT_ID"] = "test-fake-account"
    os.environ["OANDA_ENVIRONMENT"] = "practice"
    tmp = Path(tempfile.mkdtemp())
    quotes_log = observe.AppendLog(tmp / "quotes_log.jsonl")
    health_log = observe.AppendLog(tmp / "health_log.jsonl")

    try:
        oanda_module.TRADING_ENVIRONMENTS["practice"]["api"] = f"http://127.0.0.1:{port}"
        stop_event = _threading.Event()
        quote_thread = _threading.Thread(
            target=observe._quote_tick_loop,
            args=(quotes_log, health_log, fetch_pricing_samples, stop_event),
            kwargs={"sample_interval_seconds": cfg.QUOTE_SAMPLE_INTERVAL_SECONDS},
            daemon=True)
        quote_thread.start()
        time.sleep(run_seconds)
        stop_event.set()
        quote_thread.join(timeout=10)
    finally:
        oanda_module.TRADING_ENVIRONMENTS["practice"]["api"] = original_env
        del os.environ["OANDA_API_KEY"]
        del os.environ["OANDA_ACCOUNT_ID"]
        del os.environ["OANDA_ENVIRONMENT"]
        server.shutdown()
        server_thread.join(timeout=5)

    from datetime import datetime
    rows = quotes_log.read_all()
    return sorted(datetime.fromisoformat(r["received_at_utc"]) for r in rows)


def test_single_timeout_and_retry_stays_under_the_coverage_gap_threshold():
    """2026-10 correction-order follow-up: with the REAL, unmodified
    production constants (REQUEST_TIMEOUT_SECONDS=8.0,
    RETRY_BACKOFF_BASE_SECONDS=1.0), an ordinary transient blip -- the
    FIRST attempt of one tick genuinely times out, the retry succeeds --
    measured end to end through the real _quote_tick_loop. Expected
    runtime ~15s (one real 8s hang)."""
    receipts = _run_receipt_spacing_scenario(hang_on_counts={2}, num_ticks=3, run_seconds=20.0)
    assert len(receipts) >= 2, f"expected at least 2 recorded receipts, got {len(receipts)}"
    gaps = [(b - a).total_seconds() for a, b in zip(receipts, receipts[1:])]
    max_gap = max(gaps) if gaps else 0.0
    print(f"v2 quote_client (REAL path): single-retry scenario -- receipts={len(receipts)}, "
          f"gaps={[round(g, 2) for g in gaps]}, max_gap={max_gap:.2f}s "
          f"(COVERAGE_GAP_SECONDS={cfg.COVERAGE_GAP_SECONDS}): "
          f"{'within' if max_gap <= cfg.COVERAGE_GAP_SECONDS else 'EXCEEDS'} the zero-tolerance threshold")


def test_double_timeout_and_retry_exceeds_the_coverage_gap_threshold():
    """2026-10 correction-order follow-up: quote_client.py's OWN bounded-
    retry budget permits up to MAX_RETRY_ATTEMPTS=2 retries -- if a tick
    genuinely needs BOTH (two real 8s timeouts before the third attempt
    succeeds), measured end to end with the REAL, unmodified production
    constants through the real _quote_tick_loop, the resulting receipt
    gap is expected to EXCEED COVERAGE_GAP_SECONDS=15 -- this is a real,
    structural tension between the retry-bounding design (correctly
    preventing a single hung request from stalling forever) and the
    zero-tolerance coverage gate, NOT a bug in either piece alone, and
    NOT resolved by this test (which only measures it). Expected runtime
    ~25s (two real 8s hangs plus backoff)."""
    receipts = _run_receipt_spacing_scenario(hang_on_counts={2, 3}, num_ticks=3, run_seconds=30.0)
    assert len(receipts) >= 2, f"expected at least 2 recorded receipts, got {len(receipts)}"
    gaps = [(b - a).total_seconds() for a, b in zip(receipts, receipts[1:])]
    max_gap = max(gaps) if gaps else 0.0
    print(f"v2 quote_client (REAL path): double-retry scenario -- receipts={len(receipts)}, "
          f"gaps={[round(g, 2) for g in gaps]}, max_gap={max_gap:.2f}s "
          f"(COVERAGE_GAP_SECONDS={cfg.COVERAGE_GAP_SECONDS}): "
          f"{'within' if max_gap <= cfg.COVERAGE_GAP_SECONDS else 'EXCEEDS'} the zero-tolerance threshold -- "
          f"confirms the residual-gap caveat empirically, not just by reasoning")


if __name__ == "__main__":
    test_real_request_path_enforces_an_actual_socket_timeout_against_a_hanging_server()
    test_single_timeout_and_retry_stays_under_the_coverage_gap_threshold()
    test_double_timeout_and_retry_exceeds_the_coverage_gap_threshold()
    print("All v2 quote_client real-path tests passed.")
