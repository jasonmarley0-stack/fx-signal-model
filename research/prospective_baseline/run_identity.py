"""Activation integrity: practice-only enforcement and run-identity/source-
hash tracking, so a code or contract change is never silently mixed into
an existing observation log under the same run. Both checks are enforced
in CODE (raise, refuse to proceed) -- not README instructions alone.
"""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Relative to this file's parent (research/prospective_baseline/) and the
# repo's src/ -- the exact set of files whose content determines what this
# run's decisions/scores actually mean. Any change to any of these is a
# contract/code change, not a data anomaly.
TRACKED_FILES = [
    Path(__file__).parent / "contract.py",
    Path(__file__).parent / "observe.py",
    Path(__file__).parent / "score.py",
    Path(__file__).parent.parent.parent / "src" / "combiner.py",
    Path(__file__).parent.parent.parent / "src" / "strategies" / "composite.py",
    Path(__file__).parent.parent.parent / "src" / "spreads.py",
]


class PracticeEnvironmentError(RuntimeError):
    pass


class RunIdentityMismatchError(RuntimeError):
    pass


def enforce_practice_environment(env: dict | None = None) -> None:
    """Raises PracticeEnvironmentError unless OANDA_ENVIRONMENT is
    EXACTLY 'practice' -- missing, 'live', or any other value all refuse.
    This is the code-level guard requirement 6 asks for; README
    instructions alone are not relied on."""
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
        "tracked_files": [str(p) for p in TRACKED_FILES],
    }


def load_or_create_manifest(log_dir: Path) -> dict:
    """First run in a fresh log directory: creates and persists a new
    manifest. Any subsequent run: compares the CURRENT source hash against
    the persisted one. A match resumes the same run_id silently. A
    mismatch REFUSES to proceed (raises RunIdentityMismatchError) rather
    than silently blending data collected under different code/contract
    versions into the same logs -- an intentional change must start a new
    log directory (a new run), not continue in place."""
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
