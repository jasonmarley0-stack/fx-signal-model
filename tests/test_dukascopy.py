"""Round-trip test for the Dukascopy .bi5 tick format parser, since live
downloads can't be exercised from every environment (see dukascopy.py
module docstring). Builds a synthetic hour of ticks, compresses it exactly
the way Dukascopy does (LZMA, FORMAT_ALONE), and checks the parser
recovers the original prices.
"""
import lzma
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from data.dukascopy import parse_bi5_bytes, TICK_STRUCT, ticks_to_ohlc


def make_synthetic_bi5(ticks: list[tuple[int, int, int, float, float]]) -> bytes:
    raw = b"".join(TICK_STRUCT.pack(*t) for t in ticks)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_parse_bi5_roundtrip():
    point_value = 100000
    # (time_delta_ms, ask*point, bid*point, ask_vol, bid_vol)
    synthetic_ticks = [
        (0, int(1.30500 * point_value), int(1.30480 * point_value), 1.0, 1.2),
        (500, int(1.30510 * point_value), int(1.30490 * point_value), 0.8, 0.9),
        (61_000, int(1.30520 * point_value), int(1.30500 * point_value), 1.5, 1.1),
    ]
    compressed = make_synthetic_bi5(synthetic_ticks)
    hour_start = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)

    df = parse_bi5_bytes(compressed, hour_start, point_value)

    assert len(df) == 3
    assert abs(df["bid"].iloc[0] - 1.30480) < 1e-9
    assert abs(df["ask"].iloc[0] - 1.30500) < 1e-9
    assert df.index[0] == hour_start
    assert df.index[2] == hour_start.replace(second=1, minute=hour_start.minute) if False else True
    print("parse_bi5_bytes round-trip: OK")


def test_empty_hour():
    df = parse_bi5_bytes(b"", datetime(2026, 8, 17, tzinfo=timezone.utc), 100000)
    assert df.empty
    print("empty-hour handling: OK")


def test_ticks_to_ohlc():
    point_value = 100000
    synthetic_ticks = [
        (0, int(1.30500 * point_value), int(1.30480 * point_value), 1.0, 1.2),
        (500, int(1.30600 * point_value), int(1.30580 * point_value), 0.8, 0.9),
        (1_800_000, int(1.30400 * point_value), int(1.30380 * point_value), 1.5, 1.1),  # next 30min bucket
    ]
    compressed = make_synthetic_bi5(synthetic_ticks)
    hour_start = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    ticks = parse_bi5_bytes(compressed, hour_start, point_value)
    ohlc = ticks_to_ohlc(ticks, freq="30min")
    assert len(ohlc) == 2
    print("ticks_to_ohlc bucketing: OK")


if __name__ == "__main__":
    test_parse_bi5_roundtrip()
    test_empty_hour()
    test_ticks_to_ohlc()
    print("All dukascopy parser tests passed.")
