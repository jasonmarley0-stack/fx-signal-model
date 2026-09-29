"""Frozen configurations and evaluation contract for the offline strategy
comparison. Written and fixed BEFORE any candidate's results were
inspected (see RESULTS.md "Provenance"). Nothing in this file is tuned
after seeing an outcome -- if a later milestone wants to change any of
these, that is a new, separately-labelled configuration, not an edit to
one of these three.

All three candidates below share every contract element except their own
entry rule -- decision cadence, entry delay, entry validity, fill side,
exit-checking side, ambiguity handling, position policy, and warm-up are
identical, so any performance difference is attributable to the entry
rule, not to a difference in execution assumptions.
"""
from __future__ import annotations
from dataclasses import dataclass, field

BASELINE_COMMIT = "ef5d480"          # alert-lifecycle-prospective-scoring tip this research branch was cut from
FETCH_DATE = "2026-09-29"            # when fetch_research_data.py pulled the OANDA bid/ask dataset this replay uses

# --- Evaluation contract (identical across all three candidates) ---
DECISION_GRANULARITY = "H4"          # signals are decided once per H4 bar close, matching the live scanner's cadence
OUTCOME_GRANULARITY = "M30"          # entry/exit checked at M30 resolution, matching performance_scorer.py's existing precedent
ENTRY_VALIDITY_HOURS = 4.0           # "do not enter after this time" -- one H4 bar's worth; a fixed, stated choice, not tuned
MAX_HOLDING_TIME_HOURS = 30.0        # matches performance_scorer.py's MAX_LOOKAHEAD_HOURS, for continuity, not re-validated here
ENTRY_TOLERANCE_ATR_MULTIPLE = 0.10  # entry condition = decision bar's own close +/- this fraction of ATR
WARMUP_H4_BARS = 60                  # ~10 days -- enough for EMA50/ATR14 to be real before any candidate may fire

# Position policy: an existing OPEN (assumed-entered, unresolved) position
# in a pair prevents a new entry in that same pair under the same
# candidate. A new signal that arrives while a position is open is
# recorded as suppressed, not silently dropped and not turned into a
# second concurrent position. This is a policy CHOICE, stated explicitly
# because the task requires it to be, not derived from any existing code.
ONE_OPEN_POSITION_PER_PAIR = True

# Cost accounting: a long's entry is checked/filled against the ASK series
# (a buyer pays the ask); its stop/target/time-exit are checked/priced
# against the BID series (closing a long = selling = receiving the bid).
# A short is the mirror image (entry vs BID, exit vs ASK). This charges
# the full real spread exactly once, embedded naturally in the gap
# between the entry and exit prices -- no separate spread deduction is
# ever applied on top of this, which would double-count it.
FILL_SIDE = {"long": {"entry": "ask", "exit": "bid"}, "short": {"entry": "bid", "exit": "ask"}}

DEV_HOLDOUT_NOTE = (
    "DEV_START..DEV_END and HOLDOUT_START..HOLDOUT_END are defined in data_loader.py, "
    "fixed before any candidate was run. WARMUP_START..DEV_START (2025-10-01..2025-11-15, "
    "~45 days / 270 H4 bars) is additional history so every candidate's indicators are real "
    "by the first bar that may fire in DEV -- it is not itself an evaluation period."
)


@dataclass(frozen=True)
class CandidateConfig:
    name: str
    scanner_version: str          # tag written into every hypothetical alert_lifecycle event for this candidate
    description: str
    strategy_module: str          # informational -- which research/offline_comparison/strategies/*.py implements it
    confidence_medium_threshold: float | None   # None where not applicable (trend_pullback has no magnitude to tier)
    confidence_high_threshold: float | None
    strong_agreement_threshold: float | None
    rr_medium: float
    rr_strong: float | None


CANDIDATES: dict[str, CandidateConfig] = {
    "baseline": CandidateConfig(
        name="baseline",
        scanner_version="REPLAY_H4_majors_baseline_v1",
        description=("The current H4 baseline exactly as deployed, frozen to commit " + BASELINE_COMMIT + ": "
                      "src/strategies/composite.py's default weights (orb 0.5 / trend 0.3 / pattern 0.2), "
                      "src/combiner.py's thresholds and reward:risk tiers, unmodified. ORB is a real input to "
                      "this candidate's formula but is confirmed structurally 0 on H4 data (see "
                      "CODEX_FOLLOWUP_FINDINGS.md) -- this candidate is run AS-IS, defect included, because that "
                      "is genuinely what is deployed."),
        strategy_module="src/strategies/composite.py + src/combiner.py (unmodified, imported directly)",
        confidence_medium_threshold=0.35, confidence_high_threshold=0.6, strong_agreement_threshold=0.75,
        rr_medium=1.5, rr_strong=2.0,
    ),
    "genuine_orb": CandidateConfig(
        name="genuine_orb",
        scanner_version="REPLAY_genuine_orb_v1",
        description=("Identical composite formula and combiner thresholds to baseline, with ONE change: the orb "
                      "input is computed from a genuinely-measured M30 opening range (real bars, available only "
                      "after the opening M30 bar closes), not the always-zero H4-relabelled version. Tests "
                      "whether repairing the ORB component (rather than removing or replacing it -- see "
                      "RESEARCH_SPEC_ALERT_OUTCOME_AND_ORB.md hypothesis H-A) changes anything."),
        strategy_module="research/offline_comparison/strategies/genuine_orb.py",
        confidence_medium_threshold=0.35, confidence_high_threshold=0.6, strong_agreement_threshold=0.75,
        rr_medium=1.5, rr_strong=2.0,
    ),
    "trend_pullback": CandidateConfig(
        name="trend_pullback",
        scanner_version="REPLAY_trend_pullback_v1",
        description=("A structurally different, simple H4 strategy: trend-pullback continuation (EMA20/EMA50 "
                      "trend filter, entry on a pullback-to-EMA20-then-resume pattern). One fixed rule set, "
                      "documented in research/offline_comparison/strategies/trend_pullback.py, chosen before "
                      "evaluation. No confidence tiers (single fixed 'medium' label), no PESTLE, no sweep."),
        strategy_module="research/offline_comparison/strategies/trend_pullback.py",
        confidence_medium_threshold=None, confidence_high_threshold=None, strong_agreement_threshold=None,
        rr_medium=1.0, rr_strong=None,
    ),
}
