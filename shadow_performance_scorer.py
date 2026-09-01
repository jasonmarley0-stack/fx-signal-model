"""Scores shadow_signals_log/ (see shadow_scanner.py) against real OANDA
price action, exactly the same way performance_scorer.py scores the live
product's signals — reuses its scoring functions directly rather than
reimplementing them, so the shadow track record is measured on identical
terms to the live one and the two numbers are actually comparable.

Writes shadow_performance.json, parallel to performance.json. Meant to run
under systemd on a recurring timer (see
setup/shadow-performance-scorer.service + .timer), not interactively.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from performance_scorer import score_trade_outcome, score_directional, aggregate  # noqa: E402

SHADOW_LOG_DIR = Path(__file__).parent / "shadow_signals_log"
SHADOW_PERFORMANCE_PATH = Path(__file__).parent / "shadow_performance.json"


def load_all_shadow_signals() -> list[dict]:
    if not SHADOW_LOG_DIR.exists():
        return []
    raw = []
    for log_path in sorted(SHADOW_LOG_DIR.glob("*.jsonl")):
        for line in log_path.read_text().splitlines():
            if line.strip():
                raw.append(json.loads(line))
    # shadow entries never carry a window (see shadow_scanner.log_shadow_signal),
    # so unlike performance_scorer.load_all_signals there's no generated_at_utc to
    # key on — logged_at is already unique per genuine fire (shadow_scanner.py
    # dedups at the source via its own state file), so it's a safe key here.
    seen: dict[tuple, dict] = {}
    for e in raw:
        key = (e["pair"], e["direction"], e["confidence"], e["logged_at"])
        seen[key] = e
    return list(seen.values())


def main() -> None:
    entries = load_all_shadow_signals()
    print(f"{len(entries)} shadow signal instance(s).")

    scored = []
    for e in entries:
        try:
            outcome = score_trade_outcome(e)
        except Exception as ex:  # noqa: BLE001
            print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} — trade-outcome scoring ERROR: {ex}")
            outcome = {"stop": None, "target": None, "outcome": "no_data", "exit_price": None, "r_multiple": None}
        try:
            directional = score_directional(e)
        except Exception as ex:  # noqa: BLE001
            print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} — directional scoring ERROR: {ex}")
            directional = {"directional_horizon_hours": None, "directional_outcome": "no_data", "directional_price": None}

        merged = dict(e)
        merged.update(outcome)
        merged.update(directional)
        scored.append(merged)

        r_str = f" ({outcome['r_multiple']:+.2f}R)" if outcome.get("r_multiple") is not None else ""
        print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} -> {outcome['outcome']}{r_str} "
              f"| directional: {directional['directional_outcome']}")

    now = datetime.now(timezone.utc)
    payload = {
        "updated_at": now.isoformat(),
        "config": "H1, reweighted ORB/trend/pattern (0.35/0.40/0.25), entry_threshold=0.40, tech-only (no PESTLE)",
        "signals": scored,
        "aggregates": {
            "7d": aggregate(scored, since=now - timedelta(days=7)),
            "30d": aggregate(scored, since=now - timedelta(days=30)),
            "all": aggregate(scored, since=None),
        },
    }
    tmp_path = SHADOW_PERFORMANCE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(SHADOW_PERFORMANCE_PATH)

    all_agg = payload["aggregates"]["all"]
    print(f"\nWrote {SHADOW_PERFORMANCE_PATH} — {len(scored)} signal(s), "
          f"{all_agg['resolved_signals']} resolved, "
          f"{all_agg['directional_sample_size']} directional-scored.")


if __name__ == "__main__":
    main()
