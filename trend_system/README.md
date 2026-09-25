# Trend-following prototype (side-by-side comparison track)

Started 2026-09-25, alongside a discussion of what a trading strategy would
look like built with no anchor to the FX signal product's existing design —
not a replacement in progress, a genuinely separate track for direct
comparison. See the parent repo's `NEXT_STEPS.md` for the fuller
conversation context.

**Deliberately different from `live_scanner.py` in every structural way:**

| | FX signal product | This prototype |
|---|---|---|
| Universe | 7 FX majors | FX majors + metals + equity indices |
| Bar/horizon | H4 (hours) | Daily (weeks-to-months holds) |
| Signal | ORB + trend + pattern composite | EMA50/EMA200 crossover only |
| Exit | Fixed SL/TP bracket | Next crossover (trend-following convention) |
| Sizing | Fixed R-multiple risk | Volatility-targeted (inverse ATR) |
| Cost modeling | Added in week 4, after the fact | Built in from the first backtest |

The bet being tested isn't "a cleverer entry signal" — it's whether
diversification across genuinely uncorrelated markets, held long enough
that transaction costs stop dominating, produces a more durable edge than
a higher-frequency single-asset-class signal does. This is the same
question the FX side has been answering empirically the hard way; this
prototype is built to answer honestly from the start rather than
discover the same problems three weeks in.

Run: `python3 backtest.py` (needs `OANDA_API_KEY`/`OANDA_ACCOUNT_ID` in the
environment, same as the rest of the repo).
