"""WP3: targeted, bounded-memory, single-pass investigation of V1's
quotes_log.jsonl for exactly the intervals that still need it --
v2-020's 49 remaining mid_scan gaps, v2-022's one ambiguous
trailing_to_deadline gap, plus a small independent-verification sample
drawn from Codex's own already-completed rows (different pairs/dates,
to spot-check trustworthiness rather than accept it blindly).

Single sequential pass over the whole file (never re-opened, never
re-scanned per interval or per trade); per-row cost is a bisect lookup
against that row's own pair's small interval list, not a scan of all
intervals. Only timestamp/bid/ask/tradeable fields are ever held, one
JSON row at a time -- no log content is buffered beyond that.

Never merges a V1 result into V2 evidence; only ever reported back as
its own, separately labelled column.
"""
import bisect
import json
import sys
from datetime import datetime, timezone

V1_LOG = "/root/fx-signal-model-stage2-observer/research/prospective_baseline/logs/quotes_log.jsonl"


def parse_t(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def load_intervals(register_rows):
    """register_rows: list of (trade_id, pair, gap_number, gap_phase, start_iso, end_iso).
    Returns {pair: [(start, end, trade_id, gap_number, gap_phase), ...]} sorted by start."""
    by_pair = {}
    for trade_id, pair, gap_number, gap_phase, start_iso, end_iso in register_rows:
        a, b = parse_t(start_iso), parse_t(end_iso)
        if a is None or b is None:
            continue
        by_pair.setdefault(pair, []).append((a, b, trade_id, gap_number, gap_phase))
    for pair in by_pair:
        by_pair[pair].sort(key=lambda x: x[0])
    return by_pair


def run(intervals_by_pair, log_path=V1_LOG):
    starts_by_pair = {p: [iv[0] for iv in ivs] for p, ivs in intervals_by_pair.items()}
    results = {}  # (trade_id, gap_number) -> first match dict, or None
    for p, ivs in intervals_by_pair.items():
        for a, b, trade_id, gap_number, gap_phase in ivs:
            results[(trade_id, gap_number)] = None

    rows_scanned = 0
    pair_rows_checked = 0
    with open(log_path) as f:
        for line in f:
            rows_scanned += 1
            if not line.strip():
                continue
            try:
                q = json.loads(line)
            except ValueError:
                continue
            pair = q.get("pair")
            ivs = intervals_by_pair.get(pair)
            if not ivs:
                continue
            t = parse_t(q.get("received_at_utc"))
            if t is None:
                continue
            pair_rows_checked += 1
            starts = starts_by_pair[pair]
            # candidate intervals: the one just before/at t (bisect), check a
            # small neighborhood since intervals can be adjacent/overlapping
            idx = bisect.bisect_right(starts, t) - 1
            for j in (idx, idx + 1):
                if j < 0 or j >= len(ivs):
                    continue
                a, b, trade_id, gap_number, gap_phase = ivs[j]
                if a <= t <= b:
                    key = (trade_id, gap_number)
                    existing = results.get(key)
                    if existing is None or t < existing["received_at_utc_parsed"]:
                        try:
                            bid, ask = float(q.get("bid")), float(q.get("ask"))
                            tradeable = q.get("tradeable")
                        except (TypeError, ValueError):
                            continue
                        results[key] = {
                            "received_at_utc": q.get("received_at_utc"),
                            "received_at_utc_parsed": t,
                            "oanda_time_utc": q.get("oanda_time_utc"),
                            "bid": bid, "ask": ask, "tradeable": tradeable,
                        }
    for v in results.values():
        if v is not None:
            v.pop("received_at_utc_parsed", None)
    return results, rows_scanned, pair_rows_checked
