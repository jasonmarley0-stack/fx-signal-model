"""One command to reproduce this milestone's offline strategy comparison.

    .venv/bin/python3 research/offline_comparison/run_experiment.py

Reads the frozen local dataset (research/offline_comparison/data/raw/,
fetched once via fetch_research_data.py from the existing, authorised
OANDA connection — see its own header for exact provenance), runs all
three frozen candidates (configs.py) across all 7 pairs and the full
WARMUP..HOLDOUT_END window, and writes everything under
research/offline_comparison/output/: the full ledger, per-candidate/
per-period summaries, by-month and by-pair breakdowns, cost/delay
sensitivity, and equity/drawdown plots.

Every alert in the ledger is a REPLAY — hypothetical, scored against
historical data, never implying a real subscriber received or acted on
it. Nothing here places an order, changes live configuration, or updates
the public performance display.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path
from datetime import timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from data_loader import load_all_pairs, PAIRS, WARMUP_START, DEV_START, DEV_END, HOLDOUT_START, HOLDOUT_END  # noqa: E402
import configs as cfg  # noqa: E402
from replay_engine import run_replay  # noqa: E402
import metrics as m  # noqa: E402
import sensitivity as sens  # noqa: E402

OUT_DIR = Path(__file__).parent / "output"


def run_all_candidates(pair_data_by_pair: dict, dataset_end) -> list[dict]:
    ledger = []
    for candidate in cfg.CANDIDATES:
        for pair in PAIRS:
            print(f"  replaying {candidate} / {pair} ...")
            ledger.extend(run_replay(candidate, pair_data_by_pair[pair], dataset_end))
    return ledger


def make_plots(candidate: str, dev_rows: list[dict], holdout_rows: list[dict], out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dev_curve = m.cumulative_r_and_drawdown(dev_rows)
    holdout_curve = m.cumulative_r_and_drawdown(holdout_rows)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), sharex=False,
                                    gridspec_kw={"height_ratios": [2, 1]})
    offset = 0.0
    for label, curve, color in (("dev", dev_curve, "tab:blue"), ("holdout", holdout_curve, "tab:orange")):
        if curve.empty:
            continue
        x = range(len(curve))
        ax1.plot(x, curve["cumulative_r"] + offset, label=f"{label} (n={len(curve)})", color=color)
        ax2.fill_between(x, curve["drawdown_r"], 0, color=color, alpha=0.5, step=None)
    ax1.set_title(f"{candidate}: cumulative net R (resolved trades, chronological by exit)")
    ax1.set_ylabel("Cumulative R")
    ax1.legend()
    ax1.axhline(0, color="gray", linewidth=0.5)
    ax2.set_title("Drawdown (R)")
    ax2.set_ylabel("Drawdown R")
    ax2.set_xlabel("Resolved trade #")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    print("Loading pair data...")
    pairs = load_all_pairs()
    dataset_end = HOLDOUT_END.astimezone(timezone.utc)

    print("Running replays (3 candidates x 7 pairs)...")
    ledger = run_all_candidates(pairs, dataset_end)
    ledger_df = pd.DataFrame(ledger)
    ledger_df.to_csv(OUT_DIR / "ledger_full.csv", index=False)
    print(f"Wrote {len(ledger_df)} ledger rows to {OUT_DIR / 'ledger_full.csv'}")

    summary_rows = []
    for candidate in cfg.CANDIDATES:
        for period_name, start, end in (("dev", DEV_START, DEV_END), ("holdout", HOLDOUT_START, HOLDOUT_END),
                                         ("full", DEV_START, HOLDOUT_END)):
            rows = m.candidate_ledger(ledger, candidate, start, end)
            s = m.summarize(rows)
            bounds = m.unknown_outcome_bounds(rows)
            curve = m.cumulative_r_and_drawdown(rows)
            max_dd = curve["drawdown_r"].min() if not curve.empty else None
            summary_rows.append({"candidate": candidate, "period": period_name, **s,
                                  "max_drawdown_r": max_dd,
                                  "unknown_best_case_avg_r": bounds["best_case_avg_r"],
                                  "unknown_worst_case_avg_r": bounds["worst_case_avg_r"]})

        by_month_df = m.by_month(m.candidate_ledger(ledger, candidate, DEV_START, HOLDOUT_END),
                                  period_start=DEV_START, period_end=HOLDOUT_END)
        by_month_df.to_csv(OUT_DIR / f"by_month_{candidate}.csv", index=False)
        by_pair_df = m.by_pair(m.candidate_ledger(ledger, candidate, DEV_START, HOLDOUT_END))
        by_pair_df.to_csv(OUT_DIR / f"by_pair_{candidate}.csv", index=False)

        dev_rows = m.candidate_ledger(ledger, candidate, DEV_START, DEV_END)
        holdout_rows = m.candidate_ledger(ledger, candidate, HOLDOUT_START, HOLDOUT_END)
        make_plots(candidate, dev_rows, holdout_rows, OUT_DIR / f"equity_{candidate}.png")

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_DIR / "summary_by_candidate_period.csv", index=False)
    print(summary_df.to_string(index=False))

    print("Running cost/delay sensitivity (full window, issued versions only)...")
    sensitivity_rows = []
    for candidate in cfg.CANDIDATES:
        rows = m.candidate_ledger(ledger, candidate, DEV_START, HOLDOUT_END)
        issued = [r for r in rows if r["event_type"] in ("issued", "revised")]
        base_r = [r["r_multiple"] for r in issued if r.get("r_multiple") is not None]
        base_avg = sum(base_r) / len(base_r) if base_r else None

        mid_scored = sens.rescored_with_mid(issued, pairs, dataset_end)
        mid_r = [r["r_multiple"] for r in mid_scored if r.get("r_multiple") is not None]
        mid_avg = sum(mid_r) / len(mid_r) if mid_r else None

        delay_scored = sens.rescored_with_extra_delay(issued, pairs, dataset_end, extra_minutes=30)
        delay_r = [r["r_multiple"] for r in delay_scored if r.get("r_multiple") is not None]
        delay_avg = sum(delay_r) / len(delay_r) if delay_r else None

        sensitivity_rows.append({
            "candidate": candidate,
            "avg_r_real_bid_ask": base_avg, "n_real": len(base_r),
            "avg_r_zero_cost_mid": mid_avg, "n_mid": len(mid_r),
            "cost_impact_r": (base_avg - mid_avg) if (base_avg is not None and mid_avg is not None) else None,
            "avg_r_plus_30min_delay": delay_avg, "n_delay": len(delay_r),
            "delay_impact_r": (delay_avg - base_avg) if (base_avg is not None and delay_avg is not None) else None,
        })
    sensitivity_df = pd.DataFrame(sensitivity_rows)
    sensitivity_df.to_csv(OUT_DIR / "sensitivity.csv", index=False)
    print(sensitivity_df.to_string(index=False))

    print(f"\nAll outputs written to {OUT_DIR}")


if __name__ == "__main__":
    main()
