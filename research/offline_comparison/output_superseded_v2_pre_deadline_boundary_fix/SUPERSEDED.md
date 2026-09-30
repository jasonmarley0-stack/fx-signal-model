# Superseded — do not use for conclusions

This is the output of commit `819c41b` (the first correction pass), before the deadline-boundary fix.

**Remaining defect found and fixed after this output was generated:** the completed-candle stop/target scan included a candle whose OWN OPEN was at-or-before the holding deadline but whose FULL SPAN (open + interval) extended past it — letting that candle's high/low, which partly reflects price action *after* the deadline, resolve a stop/target/ambiguity decision that should have been bounded by the deadline. Fixed in `replay_scorer.py`: only fully-completed candles may contribute a stop/target hit via high/low; the single candle whose open lands exactly on the deadline may only contribute via its own open (a gap check, or the time-exit price); without an exact-deadline quote, the outcome is `incomplete_coverage`, never a fabricated fill from an earlier stale quote.

See `../RESULTS.md` for the corrected numbers and `../ISSUES.md` for the full record.
