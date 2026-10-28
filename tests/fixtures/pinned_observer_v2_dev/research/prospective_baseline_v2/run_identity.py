"""Activation integrity for v2 -- unchanged mechanism from v1 (see v1's own
run_identity.py), pointed at v2's own tracked files. A v1 run and a v2 run
are tracked under completely separate manifests in completely separate log
directories; this module has no way to blend them even by accident -- it
only ever reads/writes log_dir/run_manifest.json for whatever log_dir it's
given.
"""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"

# v2's own contract/collector/scorer/report/client/identity files, plus the
# same frozen strategy import graph v1 tracks -- deliberately over-inclusive,
# same reasoning as v1's own TRACKED_FILES.
TRACKED_FILES = [
    Path(__file__).parent / "contract.py",
    Path(__file__).parent / "observe.py",
    Path(__file__).parent / "score.py",
    Path(__file__).parent / "report.py",
    Path(__file__).parent / "quote_client.py",
    Path(__file__).parent / "run_identity.py",
    _SRC / "combiner.py",
    _SRC / "spreads.py",
    _SRC / "data" / "oanda.py",
    _SRC / "strategies" / "composite.py",
    _SRC / "strategies" / "indicators.py",
    _SRC / "strategies" / "opening_range_breakout.py",
    _SRC / "strategies" / "trend_following.py",
    _SRC / "strategies" / "candlestick_patterns.py",
]

CONTRACT_VERSION = "v2"


class PracticeEnvironmentError(RuntimeError):
    pass


class RunIdentityMismatchError(RuntimeError):
    pass


def enforce_practice_environment(env: dict | None = None) -> None:
    env = env if env is not None else os.environ
    value = env.get("OANDA_ENVIRONMENT")
    if value != "practice":
        raise PracticeEnvironmentError(
            f"OANDA_ENVIRONMENT must be exactly 'practice' to run the prospective observer — "
            f"got {value!r}. This system must never point at a live/funded account. Refusing to start."
        )


def compute_source_hash(paths: list[Path] | None = None) -> str:
    paths = paths if paths is not None else TRACKED_FILES
    h = hashlib.sha256()
    for p in sorted(paths, key=str):
        h.update(str(p.name).encode())
        h.update(p.read_bytes() if p.exists() else b"<MISSING>")
    return h.hexdigest()


def build_manifest(run_id: str | None = None) -> dict:
    return {
        "run_id": run_id or uuid.uuid4().hex,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_hash": compute_source_hash(),
        "contract_version": CONTRACT_VERSION,
        "tracked_files": [str(p) for p in TRACKED_FILES],
    }


def load_or_create_manifest(log_dir: Path) -> dict:
    """Identical discipline to v1: a missing manifest is created once; an
    existing one is verified, never silently overwritten. A hash mismatch
    refuses to proceed."""
    manifest_path = log_dir / "run_manifest.json"
    current_hash = compute_source_hash()
    if not manifest_path.exists():
        manifest = build_manifest()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return manifest

    persisted = json.loads(manifest_path.read_text())
    if persisted.get("source_hash") != current_hash:
        raise RunIdentityMismatchError(
            f"Source/contract hash mismatch in {log_dir}: this run was started under hash "
            f"{persisted.get('source_hash')!r} (run_id={persisted.get('run_id')}), but the code now "
            f"present hashes to {current_hash!r}. Refusing to silently continue this run under changed "
            f"code. Start a new observation log directory (a new run) for the changed code, or restore "
            f"the code this run was started under."
        )
    return persisted
