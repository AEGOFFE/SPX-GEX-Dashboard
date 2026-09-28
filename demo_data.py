"""
Demo mode: load a snapshot written by snapshot.py.

Snapshots (schema 2) hold ONLY the tool's calculated outputs - net GEX by strike, totals,
flip level, the gamma/charm surfaces - plus the SPX index level and its 10-minute path.
No per-contract option data (gamma, open interest, implied volatility) is stored.
"""
import glob
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


def latest_snapshot_path():
    """SNAPSHOT_FILE env var if set, otherwise the newest file in snapshots/."""
    path = os.getenv("SNAPSHOT_FILE")
    if path:
        return path
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "spx_0dte_*.json")))
    return files[-1] if files else None


def load_snapshot(path=None):
    """Return the same results dict that gex.compute_results() builds from live data."""
    path = path or latest_snapshot_path()
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        snap = json.load(f)
    if snap.get("schema") != 2:
        raise ValueError(f"Unsupported snapshot schema {snap.get('schema')} in {os.path.basename(path)}. "
                         "Re-run snapshot.py to create a new one.")

    surfaces = {}
    for name in ("gamma", "charm"):
        s = (snap.get("surfaces") or {}).get(name)
        surfaces[name] = None if not s else (
            np.array(s["prices"]), pd.DatetimeIndex(pd.to_datetime(s["times"])), np.array(s["z"]))

    path_rows = snap.get("price_path") or []
    candles = None
    if path_rows:
        candles = pd.DataFrame(path_rows)
        candles["datetime"] = pd.to_datetime(candles["t"])      # naive ET, same as live mode
        candles = candles[["datetime", "close"]]

    as_of = datetime.fromisoformat(snap["captured_at"])
    t = snap["totals"]
    return {
        "spot": float(snap["spot"]),
        "as_of": as_of,
        "n_contracts": snap.get("n_contracts"),
        "call_gex": t["call_gex"], "put_gex": t["put_gex"],
        "flip": snap.get("flip"),
        "strikes": pd.DataFrame(snap["strikes"]).rename(columns={"strike": "Strike", "net_gex": "GEX"}),
        "surfaces": surfaces,
        "price_date": datetime.fromisoformat(snap["session_date"]).date(),
        "candles": candles,
        # extras used only for the demo banner
        "sample": bool(snap.get("sample")),
        "candles_source": snap.get("price_path_source") or "no intraday path in snapshot",
        "file": os.path.basename(path),
    }
