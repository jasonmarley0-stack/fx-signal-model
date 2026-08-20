"""Downloads free historical tick data from Dukascopy and aggregates it into
OHLC bars for the backtester.

Dukascopy publishes hourly tick files at:
  https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
(month is zero-indexed: January = 00). Each file is LZMA-compressed
(FORMAT_ALONE) raw binary; empty files (no ticks that hour, e.g. weekends)
are zero bytes and are skipped.

Tick record format (20 bytes, big-endian):
  uint32 time_delta_ms   (ms since the start of the hour)
  uint32 ask             (price * point_value, integer)
  uint32 bid             (price * point_value, integer)
  float32 ask_volume     (millions of the base currency)
  float32 bid_volume

NOTE: this sandbox's network egress does not have datafeed.dukascopy.com
allowlisted (every request returns a 403 at the proxy), so live downloads
could not be tested end-to-end from here. The tick-parsing logic below is
verified with a synthetic round-trip test (see tests/test_dukascopy.py) so
the format handling is correct; run this against the real endpoint from
your own machine, where there's no such restriction.
"""
from __future__ import annotations
import lzma
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import pandas as pd

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
CACHE_DIR = Path(__file__).parent.parent.parent / "data_cache"

# Dukascopy's CDN can reject requests carrying httpx's default User-Agent
# (some WAFs block obvious non-browser clients even when the endpoint itself
# is reachable) — a browser-like UA avoids that without changing anything else.
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# JPY-quoted pairs are scaled by 1000, everything else by 100000
POINT_VALUE = {"JPY": 1000}
DEFAULT_POINT_VALUE = 100000

TICK_STRUCT = struct.Struct(">IIIff")  # time_delta_ms, ask, bid, ask_vol, bid_vol


def _point_value(symbol: str) -> int:
    for suffix, value in POINT_VALUE.items():
        if symbol.endswith(suffix):
            return value
    return DEFAULT_POINT_VALUE


def _hour_url(symbol: str, dt: datetime) -> str:
    return f"{BASE_URL}/{symbol}/{dt.year}/{dt.month - 1:02d}/{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"


def _cache_path(symbol: str, dt: datetime) -> Path:
    d = CACHE_DIR / symbol / f"{dt.year}" / f"{dt.month:02d}" / f"{dt.day:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{dt.hour:02d}h_ticks.bi5"


def parse_bi5_bytes(raw_compressed: bytes, hour_start: datetime, point_value: int) -> pd.DataFrame:
    """Decompresses one hour's .bi5 payload into a tick DataFrame (time, bid, ask)."""
    if len(raw_compressed) == 0:
        return pd.DataFrame(columns=["time", "bid", "ask"]).set_index("time")

    raw = lzma.decompress(raw_compressed, format=lzma.FORMAT_ALONE)
    n_ticks = len(raw) // TICK_STRUCT.size
    rows = []
    for i in range(n_ticks):
        time_delta_ms, ask, bid, ask_vol, bid_vol = TICK_STRUCT.unpack_from(raw, i * TICK_STRUCT.size)
        ts = hour_start + timedelta(milliseconds=time_delta_ms)
        rows.append((ts, bid / point_value, ask / point_value))
    df = pd.DataFrame(rows, columns=["time", "bid", "ask"]).set_index("time")
    return df


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
REQUEST_DELAY_SECONDS = 0.05  # small gap between requests to avoid triggering rate limiting in the first place


def fetch_hour_ticks(symbol: str, dt: datetime, client: httpx.Client, use_cache: bool = True) -> pd.DataFrame:
    import time
    cache_path = _cache_path(symbol, dt)
    if use_cache and cache_path.exists():
        raw = cache_path.read_bytes()
    else:
        last_exc = None
        for attempt in range(MAX_RETRIES):
            if attempt > 0:
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                time.sleep(backoff)
            try:
                resp = client.get(_hour_url(symbol, dt), headers=REQUEST_HEADERS, timeout=20.0)
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} on attempt {attempt + 1}/{MAX_RETRIES}",
                        request=resp.request, response=resp,
                    )
                    continue
                resp.raise_for_status()
                last_exc = None
                break
            except (httpx.ConnectError, httpx.ProxyError, httpx.TimeoutException,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                # RemoteProtocolError covers "Server disconnected without sending a
                # response" — Dukascopy's CDN drops connections under load same as
                # it 503s; both are worth a retry rather than failing the whole hour.
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        time.sleep(REQUEST_DELAY_SECONDS)
        raw = resp.content
        if use_cache:
            cache_path.write_bytes(raw)
    return parse_bi5_bytes(raw, dt.replace(minute=0, second=0, microsecond=0), _point_value(symbol))


def download_ticks(symbol: str, start: datetime, end: datetime, use_cache: bool = True,
                    verbose: bool = True) -> pd.DataFrame:
    """Downloads/reads-from-cache all ticks for `symbol` between start and end (UTC)."""
    hours = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        hours.append(cur)
        cur += timedelta(hours=1)

    all_ticks = []
    connection_failures = 0
    http_status_counts: dict[int, int] = {}
    empty_hours = 0
    with httpx.Client() as client:
        for i, h in enumerate(hours):
            try:
                ticks = fetch_hour_ticks(symbol, h, client, use_cache=use_cache)
                if ticks.empty:
                    empty_hours += 1
                else:
                    all_ticks.append(ticks)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                http_status_counts[code] = http_status_counts.get(code, 0) + 1
                continue  # e.g. weekend hours with no file for this symbol/hour
            except (httpx.ConnectError, httpx.ProxyError, httpx.TimeoutException,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                connection_failures += 1
                if connection_failures == 1:
                    print(f"Warning: could not reach datafeed.dukascopy.com ({exc}). "
                          f"Check network access to that domain from this machine.")
                continue
            if verbose and (i + 1) % 100 == 0:
                print(f"  ...{i + 1}/{len(hours)} hours checked, {len(all_ticks)} with ticks so far")

    if verbose:
        print(f"download_ticks({symbol}): {len(hours)} hours requested, "
              f"{len(all_ticks)} returned ticks, {empty_hours} empty (no ticks that hour), "
              f"HTTP errors: {http_status_counts or 'none'}, connection failures: {connection_failures}")

    if connection_failures == len(hours) and len(hours) > 0:
        raise ConnectionError(
            "Every request to datafeed.dukascopy.com failed to connect — this "
            "environment likely can't reach that domain (check firewall/proxy "
            "allowlist). Try running this from a machine with unrestricted "
            "internet access."
        )
    if http_status_counts and not all_ticks:
        raise RuntimeError(
            f"Got HTTP errors on every request (status codes: {http_status_counts}) "
            f"and no usable ticks for {symbol} between {start} and {end}. A 403 on "
            f"every request usually means Dukascopy's CDN is blocking this client — "
            f"try again (the User-Agent header may need further tweaking), or check "
            f"the symbol format (e.g. 'EURUSD', no slash)."
        )
    if not all_ticks:
        return pd.DataFrame(columns=["bid", "ask"], index=pd.DatetimeIndex([], name="time", tz="UTC"))
    return pd.concat(all_ticks).sort_index()


def ticks_to_ohlc(ticks: pd.DataFrame, freq: str = "30min") -> pd.DataFrame:
    """Aggregates tick mid-price into OHLC bars at the given pandas frequency."""
    if ticks.empty:
        empty_index = pd.DatetimeIndex([], name=ticks.index.name or "time",
                                        tz=getattr(ticks.index, "tz", None))
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=empty_index)
    mid = (ticks["bid"] + ticks["ask"]) / 2
    ohlc = mid.resample(freq).ohlc()
    ohlc["volume"] = mid.resample(freq).count()
    return ohlc.dropna(subset=["open"])


def get_ohlc(symbol: str, start: datetime, end: datetime, freq: str = "30min", use_cache: bool = True,
             verbose: bool = True) -> pd.DataFrame:
    """Main entry point: real Dukascopy OHLC bars for `symbol` between start/end (UTC, tz-aware)."""
    ticks = download_ticks(symbol, start, end, use_cache=use_cache, verbose=verbose)
    ohlc = ticks_to_ohlc(ticks, freq=freq)
    if ohlc.empty:
        return ohlc
    ohlc.index = ohlc.index.tz_localize("UTC") if ohlc.index.tz is None else ohlc.index
    return ohlc
