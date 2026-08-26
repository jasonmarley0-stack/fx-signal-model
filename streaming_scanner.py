"""Real-time streaming scanner: replaces scanner_oanda.py's 30-minute poll
loop with a persistent connection to OANDA's PricingStream. One stream
carries live bid/ask ticks for every default pair; each pair keeps ~300
closed M30 bars (seeded via the existing fetch_oanda_candles()) plus one
in-progress bar built live from ticks and rolled over at each :00/:30
boundary. technical_score() (unchanged) runs against history + the live bar
every ~30s — throttled, since ticks can arrive many times a second during
active sessions — so signals track current price action instead of only
fully-closed bars.

PESTLE is cached per-currency on a 5-minute timer (news evidence doesn't
move at tick speed, and every pair recompute would otherwise hit Signal
Engine twice for no reason).

A signal only fires — gets logged to signals_log/ and pushed to
alerts.json — on a genuine new transition (direction or confidence
changing), not on every 30s recompute. This mirrors monitor_signals.py's
dedupe logic, applied continuously instead of every 30 minutes. The very
first recompute for a pair each run only establishes a baseline; it never
fires, so restarting the service doesn't replay an alert for whatever
state the market was already in.

Two threads:
  - the stream thread: consumes PricingStream forever, updates each pair's
    in-progress bar on every tick, and reconnects with backoff on any drop.
  - the recompute thread: every ~30s, snapshots each pair's history + live
    bar, runs the full technical+PESTLE+combine pipeline, writes
    live_scan.json for the dashboard, and fires alerts on transitions.

They share pair state through PairState objects guarded by one lock — the
critical sections are just dict/DataFrame assignment, no I/O, so a single
lock for all pairs is simpler than per-pair locking and costs nothing.

Usage:
    python3 streaming_scanner.py [--pairs EURUSD,GBPUSD,...]

Meant to run under systemd (see setup/signal-scanner.service), not as a
one-shot — there is no --once mode, since the whole point is the standing
stream connection. For a single manual pass against real data, run
scanner_oanda.py --once instead.
"""
from __future__ import annotations
import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from oandapyV20 import API
from oandapyV20.endpoints.pricing import PricingStream

sys.path.insert(0, str(Path(__file__).parent / "src"))

import os  # noqa: E402

from data.oanda import fetch_oanda_candles, to_oanda_instrument  # noqa: E402
from dashboard_data import DEFAULT_PAIRS  # noqa: E402
from strategies.composite import technical_score  # noqa: E402
from pestle.pestle_scorer import currency_pestle_score, top_evidence  # noqa: E402
from pestle.signal_engine_client import SignalEngineClient  # noqa: E402
from combiner import combine_signal  # noqa: E402

LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
ALERTS_PATH = Path(__file__).parent / "alerts.json"
STATE_PATH = Path(__file__).parent / ".streaming_state.json"
SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"

HISTORY_BARS = 300
RECOMPUTE_SECONDS = 30
SPARKLINE_BARS = 48  # ~24h of M30 bars — the Live tab's per-pair trendline
PESTLE_CACHE_SECONDS = 5 * 60
MAX_ALERTS = 50
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]  # seconds; holds at the last value


def to_bucket(ts: pd.Timestamp) -> pd.Timestamp:
    """Floors a UTC timestamp to the start of its 30-minute bar — matches
    the boundaries fetch_oanda_candles' M30 bars already land on, so closed
    live bars append onto REST-seeded history without a seam."""
    return ts.floor("30min")


def tick_mid_price(tick: dict) -> float | None:
    """OANDA PricingStream PRICE ticks carry bids/asks (each a list of
    {"price": str, "liquidity": int}, best level first) plus convenience
    closeoutBid/closeoutAsk strings. Mid of best bid/ask, to match
    fetch_oanda_candles' price="M" (midpoint) convention."""
    try:
        bid = float(tick.get("closeoutBid") or tick["bids"][0]["price"])
        ask = float(tick.get("closeoutAsk") or tick["asks"][0]["price"])
        return (bid + ask) / 2
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class PairState:
    """Mutable per-pair state shared between the stream and recompute
    threads. `history` holds only closed bars; the live_* fields are the
    in-progress bar being built from ticks."""

    def __init__(self, pair: str, history: pd.DataFrame):
        self.pair = pair
        self.history = history
        self.live_bucket: pd.Timestamp | None = None
        self.live_open = self.live_high = self.live_low = self.live_close = None
        self.last_price: float | None = None

    def apply_tick(self, price: float, tick_time: pd.Timestamp) -> None:
        bucket = to_bucket(tick_time)
        if self.live_bucket is None:
            self.live_bucket = bucket
            self.live_open = self.live_high = self.live_low = self.live_close = price
        elif bucket > self.live_bucket:
            closed = pd.DataFrame(
                {"open": [self.live_open], "high": [self.live_high],
                 "low": [self.live_low], "close": [self.live_close]},
                index=[self.live_bucket],
            )
            self.history = pd.concat([self.history, closed]).iloc[-HISTORY_BARS:]
            self.live_bucket = bucket
            self.live_open = self.live_high = self.live_low = self.live_close = price
        else:
            self.live_high = max(self.live_high, price)
            self.live_low = min(self.live_low, price)
            self.live_close = price
        self.last_price = price

    def snapshot_df(self) -> pd.DataFrame | None:
        """History + the in-progress bar as one DataFrame ready for
        technical_score() — or None if no ticks have landed yet."""
        if self.live_bucket is None:
            return None
        live_row = pd.DataFrame(
            {"open": [self.live_open], "high": [self.live_high],
             "low": [self.live_low], "close": [self.live_close]},
            index=[self.live_bucket],
        )
        return pd.concat([self.history, live_row])


def stream_loop(pairs: list[str], pair_states: dict[str, PairState],
                 lock: threading.Lock, stop_event: threading.Event) -> None:
    api_key = os.environ.get("OANDA_API_KEY")
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    environment = os.environ.get("OANDA_ENVIRONMENT", "practice")
    if not api_key or not account_id:
        raise RuntimeError("OANDA_API_KEY / OANDA_ACCOUNT_ID not set — check setup/oanda.env is loaded")

    instrument_to_pair = {to_oanda_instrument(p): p for p in pairs}
    instruments = ",".join(instrument_to_pair)
    backoff_idx = 0

    while not stop_event.is_set():
        api = API(access_token=api_key, environment=environment)
        req = PricingStream(accountID=account_id, params={"instruments": instruments})
        try:
            print(f"[stream] connecting — {len(instrument_to_pair)} instruments ({instruments})")
            for tick in api.request(req):
                if stop_event.is_set():
                    break
                backoff_idx = 0  # any message (including a heartbeat) proves the connection is alive
                if tick.get("type") != "PRICE":
                    continue  # HEARTBEAT — nothing to update
                pair = instrument_to_pair.get(tick.get("instrument"))
                if pair is None:
                    continue
                price = tick_mid_price(tick)
                if price is None:
                    continue
                tick_time = pd.to_datetime(tick["time"], utc=True)
                with lock:
                    pair_states[pair].apply_tick(price, tick_time)
        except Exception as e:  # noqa: BLE001 — any drop should reconnect, never kill the scanner
            print(f"[stream] dropped ({e!r}) — reconnecting...")
        if stop_event.is_set():
            break
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        backoff_idx += 1
        print(f"[stream] reconnecting in {delay}s")
        time.sleep(delay)


def get_currency_pestle(currency: str, client: SignalEngineClient, cache: dict) -> tuple[float, dict, list]:
    entry = cache.get(currency)
    now = time.monotonic()
    if entry and now - entry["fetched_at"] < PESTLE_CACHE_SECONDS:
        return entry["score"], entry["categories"], entry["evidence"]
    score, cats = currency_pestle_score(currency, client=client)
    # Fetched together with the score (not separately, on-demand) so this
    # stays on the same 5-min cache cadence as the score itself — an alert
    # card's evidence should match the score it fired on, not drift ahead
    # of it between cache refreshes.
    evidence = top_evidence(currency, client=client, limit=2)
    cache[currency] = {"score": score, "categories": cats, "evidence": evidence, "fetched_at": now}
    return score, cats, evidence


def get_pair_pestle(pair: str, client: SignalEngineClient, cache: dict) -> dict:
    base, quote = pair[:3], pair[3:]
    base_score, base_cats, base_evidence = get_currency_pestle(base, client, cache)
    quote_score, quote_cats, quote_evidence = get_currency_pestle(quote, client, cache)
    pair_score = max(-1.0, min(1.0, base_score - quote_score))
    return {
        "pair": pair, "pestle_score": pair_score,
        "base": {"currency": base, "score": base_score, "categories": base_cats, "evidence": base_evidence},
        "quote": {"currency": quote, "score": quote_score, "categories": quote_cats, "evidence": quote_evidence},
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def log_signal(pair: str, sig) -> None:
    if sig.direction == "no_trade":
        return
    SIGNALS_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = SIGNALS_LOG_DIR / f"{today}.jsonl"
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "direction": sig.direction,
        "confidence": sig.confidence,
        "combined_score": sig.combined_score,
        "tech_score": sig.tech_score,
        "pestle_score": sig.pestle_score,
        "entry": sig.entry,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "reason": sig.reason,
        "window": None,
    }
    if sig.window:
        w = dict(sig.window)
        w["generated_at_utc"] = w["generated_at_utc"].isoformat()
        w["valid_until_utc"] = w["valid_until_utc"].isoformat()
        entry["window"] = w
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def push_alert(pair: str, sig, tech: dict, pestle: dict, window: dict | None) -> None:
    """Persists the full breakdown behind the fired signal, not just the
    headline — an alert card needs the technical components and PESTLE
    evidence to show *why*, not just direction/confidence/score. All of
    this was already computed by recompute_loop(); this just carries it
    through instead of discarding it (see SIGNAL_IQ_GAP_ANALYSIS.md item 1b)."""
    alerts = []
    if ALERTS_PATH.exists():
        try:
            alerts = json.loads(ALERTS_PATH.read_text()).get("alerts", [])
        except json.JSONDecodeError:
            alerts = []
    label = f"{sig.direction.upper()} ({sig.confidence}) @ {sig.entry:.5f}" if sig.direction != "no_trade" else "back to no_trade"
    entry = {
        "id": f"{int(time.time() * 1000)}-{pair}",
        "time": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "direction": sig.direction,
        "confidence": sig.confidence,
        "combined_score": sig.combined_score,
        "entry": sig.entry,
        "message": f"{pair}: {label}",
        "reason": sig.reason,
        "tech": tech,
        "pestle": pestle,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "window": window,
    }
    alerts.append(entry)
    alerts = alerts[-MAX_ALERTS:]
    tmp = ALERTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"alerts": alerts}, indent=2))
    tmp.replace(ALERTS_PATH)
    print(f"[alert] {entry['message']}")


def fire_if_new_transition(pair: str, sig, last_fired: dict, tech: dict, pestle: dict, window: dict | None) -> None:
    """Fires only on a genuine direction change (no_trade <-> long <-> short), not
    on confidence-tier movement. Confidence tier is a coarse bucket derived from
    combined_score crossing a threshold (e.g. 0.6 for "high"); when the score
    sits near that line, ordinary recompute-to-recompute noise flips the tier
    back and forth while direction never changes. Dedup used to be keyed on
    [direction, confidence], so each flip got logged as a "new" trade — inflating
    performance-tracking counts with several near-identical entries for one
    underlying call. Confirmed on 2026-08-26: EURNZD long fired 5x in 26 minutes
    with direction unchanged throughout, purely from tech score wobbling
    0.80<->0.96 back and forth across the confidence-tier line. See NEXT_STEPS.md."""
    prev = last_fired.get(pair)
    if isinstance(prev, list):  # migrate old [direction, confidence] state entries
        prev = prev[0]
    current = sig.direction
    last_fired[pair] = current
    if prev is None:
        return  # first recompute this run — establish baseline, don't alert on pre-existing state
    if prev == current:
        return
    log_signal(pair, sig)
    push_alert(pair, sig, tech, pestle, window)


def write_live_scan(now: datetime, rows: list[dict]) -> None:
    payload = {"generated_at": now.isoformat(), "rows": rows}
    tmp_path = LIVE_SCAN_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(LIVE_SCAN_PATH)  # atomic — dashboard never reads a half-written file


def recompute_loop(pairs: list[str], pair_states: dict[str, PairState], lock: threading.Lock,
                    client: SignalEngineClient, stop_event: threading.Event) -> None:
    pestle_cache: dict[str, dict] = {}
    last_fired = load_state()
    prev_price: dict[str, float] = {}

    while not stop_event.is_set():
        now = datetime.now(timezone.utc)
        rows = []
        for pair in pairs:
            with lock:
                st = pair_states[pair]
                df = st.snapshot_df()
                current_price = st.last_price
            if df is None:
                rows.append({"pair": pair, "error": "waiting for first tick"})
                continue
            if len(df) < 60:
                rows.append({"pair": pair, "error": f"not enough live history yet ({len(df)} bars)"})
                continue
            try:
                tech = technical_score(df)
                latest_tech = tech.iloc[-1]
                pestle = get_pair_pestle(pair, client, pestle_cache)
                sig = combine_signal(
                    pair=pair, entry=current_price, atr_value=latest_tech["atr"],
                    tech_score=latest_tech["tech_score"], pestle_score=pestle["pestle_score"],
                    generated_at=df.index[-1].to_pydatetime(),
                )

                arrow = "flat"
                if pair in prev_price and current_price is not None:
                    if current_price > prev_price[pair]:
                        arrow = "up"
                    elif current_price < prev_price[pair]:
                        arrow = "down"
                prev_price[pair] = current_price

                tech_dict = {"orb": latest_tech["orb"], "trend": latest_tech["trend"],
                             "pattern": latest_tech["pattern"], "composite": latest_tech["tech_score"]}
                window = None
                if sig.window:
                    window = {
                        "session": sig.window.get("session"),
                        "generated_at_gmt_str": sig.window.get("generated_at_gmt_str"),
                        "valid_until_gmt_str": sig.window.get("valid_until_gmt_str"),
                    }

                fire_if_new_transition(pair, sig, last_fired, tech_dict, pestle, window)

                sparkline = [round(float(v), 6) for v in df["close"].tail(SPARKLINE_BARS).tolist()]

                rows.append({
                    "pair": pair, "entry": current_price, "price_arrow": arrow,
                    "sparkline": sparkline,
                    "tech": tech_dict,
                    "pestle_score": pestle["pestle_score"],
                    "direction": sig.direction, "confidence": sig.confidence,
                    "combined_score": sig.combined_score,
                    "stop_loss_range": list(sig.stop_loss_range),
                    "take_profit_range": list(sig.take_profit_range),
                    "reason": sig.reason, "window": window,
                })
            except Exception as e:  # noqa: BLE001 — one pair's failure shouldn't blank the whole dashboard
                rows.append({"pair": pair, "error": str(e)})

        write_live_scan(now, rows)
        save_state(last_fired)

        slept = 0.0
        while slept < RECOMPUTE_SECONDS and not stop_event.is_set():
            time.sleep(1)
            slept += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=",".join(DEFAULT_PAIRS))
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    print(f"Seeding history for {len(pairs)} pairs from OANDA REST candles...")
    pair_states: dict[str, PairState] = {}
    for pair in pairs:
        df = fetch_oanda_candles(pair, count=HISTORY_BARS, granularity="M30")
        print(f"  {pair}: seeded {len(df)} closed M30 bars")
        pair_states[pair] = PairState(pair, df)

    client = SignalEngineClient()
    if client.mock:
        print("Signal Engine not reachable (mock mode) — is the signal-engine systemd service running "
              "and SIGNAL_ENGINE_BASE_URL set to http://127.0.0.1:8000? Continuing in mock mode anyway.")

    lock = threading.Lock()
    stop_event = threading.Event()

    def handle_signal(signum, frame):
        print(f"Received signal {signum} — shutting down...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    recompute_thread = threading.Thread(
        target=recompute_loop, args=(pairs, pair_states, lock, client, stop_event), daemon=True,
    )
    recompute_thread.start()

    print("Starting OANDA PricingStream...")
    stream_loop(pairs, pair_states, lock, stop_event)
    print("Stopped.")


if __name__ == "__main__":
    main()
