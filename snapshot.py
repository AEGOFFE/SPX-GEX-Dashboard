"""
snapshot.py - save a sanitized snapshot of today's SPX 0DTE chain for demo mode.

Run during market hours (Mon-Fri, 9:30-16:00 ET) with your Schwab credentials in .env:

    python snapshot.py                   # one snapshot now
    python snapshot.py --every 30        # one every 30 min until the 4:00 PM close
    python snapshot.py --force           # allow running outside market hours (testing)

Output: snapshots/spx_0dte_YYYY-MM-DD_HHMM.json

What goes IN the file: the tool's own calculated outputs (net GEX by strike, call/put/net
totals, zero-gamma flip, gamma and charm surfaces) plus the SPX index level and its
10-minute path. The raw option chain is used in memory for the math and never saved.
What NEVER goes in: per-contract gamma / open interest / implied vol, tokens, keys, account data.
"""
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
QUOTE_URL = "https://api.schwabapi.com/marketdata/v1/quotes"
HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"
SCHEMA_VERSION = 1
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")


# ---------- auth (reads/refreshes the same token file the app uses) ----------
def _token_file():
    path = os.getenv("SCHWAB_TOKEN_FILE")
    if not path:
        sys.exit("SCHWAB_TOKEN_FILE is not set in .env")
    return path


def _refresh(tokens):
    key, secret = os.getenv("APP_KEY"), os.getenv("APP_SECRET")
    if not key or not secret:
        sys.exit("APP_KEY / APP_SECRET missing from .env")
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        timeout=15,
    )
    if r.status_code != 200:
        sys.exit(f"Token refresh failed (HTTP {r.status_code}). "
                 "Open the app and log in again, then re-run.")
    new = r.json()
    new.setdefault("refresh_token", tokens["refresh_token"])
    new["timestamp"] = datetime.now().isoformat()
    if new.get("expires_in") is not None:
        new["expires_at"] = datetime.now().timestamp() + int(new["expires_in"]) - 60
    with open(_token_file(), "w") as f:
        json.dump(new, f)
    return new["access_token"]


def get_access_token():
    path = _token_file()
    if not os.path.exists(path):
        sys.exit("No token file yet. Open the app and log in with Schwab first.")
    with open(path) as f:
        tokens = json.load(f)
    if tokens.get("expires_at", 0) > datetime.now().timestamp():
        return tokens["access_token"]
    if "refresh_token" not in tokens:
        sys.exit("Token file has no refresh token. Log in through the app again.")
    return _refresh(tokens)


# ---------- data ----------
def _get(url, token, params):
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=20)
    if r.status_code != 200:
        # status code only: never echo the response body (it can contain request details)
        raise RuntimeError(f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code}")
    return r.json()


def fetch_spot(token):
    return float(_get(QUOTE_URL, token, {"symbols": "$SPX", "fields": "quote"})
                 ["$SPX"]["quote"]["lastPrice"])


def fetch_chain(token, today):
    """Raw 0DTE chain. Used in memory for the calculations only - never written to disk."""
    return _get(CHAIN_URL, token, {
        "symbol": "$SPX", "contractType": "ALL", "strategy": "SINGLE",
        "range": "ALL", "fromDate": today, "toDate": today,
    })


def fetch_candles(token, spot):
    """Today's 10-min bars. SPY (live) scaled to SPX level, same approach as the app."""
    now = datetime.now(ET)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    data = _get(HISTORY_URL, token, {
        "symbol": "SPY", "periodType": "day", "frequencyType": "minute",
        "frequency": 10, "startDate": int(start.timestamp() * 1000),
        "endDate": int(now.timestamp() * 1000), "needExtendedHoursData": "false",
    })
    bars = data.get("candles") or []
    if not bars:
        return [], None
    scale = spot / bars[-1]["close"] if bars[-1]["close"] else 10.0
    out = []
    for b in bars:
        t = datetime.fromtimestamp(b["datetime"] / 1000, ET)
        if t.date() != now.date() or not (9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60):
            continue
        out.append({"t": t.strftime("%Y-%m-%dT%H:%M"),
                    **{k: round(b[k] * scale, 2) for k in ("open", "high", "low", "close")}})
    return out, round(scale, 6)


# ---------- main ----------
def market_open(now):
    return now.weekday() < 5 and (9 * 60 + 30) <= now.hour * 60 + now.minute < 16 * 60


def _r(x, sig=5):
    """Round to significant figures so the file stays small."""
    return float(f"{x:.{sig}g}")


def take_snapshot():
    import pandas as pd
    from gex import compute_results, fmt_usd

    now = datetime.now(ET)
    token = get_access_token()
    spot = fetch_spot(token)
    chain = fetch_chain(token, now.strftime("%Y-%m-%d"))
    try:
        bars, scale = fetch_candles(token, spot)
    except RuntimeError as e:
        print(f"  warning: no intraday bars ({e}); saving without price path")
        bars, scale = [], None
    candles_df = None
    if bars:
        candles_df = pd.DataFrame(bars)
        candles_df["datetime"] = pd.to_datetime(candles_df["t"])

    res = compute_results(chain, spot, now, candles_df)
    if res is None:
        raise RuntimeError("Chain came back empty (holiday, or no 0DTE expiry today).")
    expiry = next(iter((chain.get("callExpDateMap") or chain.get("putExpDateMap") or {"?:0": 0}))).split(":")[0]
    del chain   # the raw chain goes no further than this function

    surfaces = {}
    for name, surf in res["surfaces"].items():
        if surf is None:
            surfaces[name] = None
            continue
        prices, times, Z = surf
        surfaces[name] = {"prices": [round(float(p), 2) for p in prices],
                          "times": [pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%S") for t in times],
                          "z": [[_r(float(v)) for v in row] for row in Z]}

    snap = {
        "schema": 2,
        "contents": "Calculated outputs only (net GEX by strike, totals, flip, gamma/charm surfaces) "
                    "plus the SPX index level. No per-contract option data is stored.",
        "captured_at": now.isoformat(timespec="seconds"),
        "symbol": "SPX",
        "expiry": expiry,
        "session_date": res["price_date"].isoformat(),
        "spot": round(spot, 2),
        "n_contracts": res["n_contracts"],
        "totals": {"call_gex": _r(res["call_gex"], 6), "put_gex": _r(res["put_gex"], 6)},
        "flip": round(res["flip"], 2) if res["flip"] is not None else None,
        "strikes": [{"strike": float(k), "net_gex": _r(float(g), 6)}
                    for k, g in zip(res["strikes"]["Strike"], res["strikes"]["GEX"])],
        "surfaces": surfaces,
        "price_path_source": f"SPY 10-min closes scaled x{scale} to SPX level" if scale else None,
        "price_path": [{"t": b["t"], "close": b["close"]} for b in bars],
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"spx_0dte_{now:%Y-%m-%d_%H%M}.json")
    with open(path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))

    flip = f"{res['flip']:,.2f}" if res["flip"] is not None else "N/A"
    print(f"[{now:%H:%M:%S} ET] saved {os.path.relpath(path)} ({os.path.getsize(path) // 1024} KB)")
    print(f"  spot {spot:,.2f} | flip {flip} | call {fmt_usd(res['call_gex'])} | "
          f"put {fmt_usd(res['put_gex'])} | net {fmt_usd(res['call_gex'] + res['put_gex'])} | "
          f"{res['n_contracts']} contracts | {len(bars)} bars")
    return path


def main():
    ap = argparse.ArgumentParser(description="Save a sanitized SPX 0DTE snapshot for demo mode.")
    ap.add_argument("--every", type=int, metavar="MIN",
                    help="repeat every MIN minutes until the 4:00 PM ET close")
    ap.add_argument("--force", action="store_true", help="run even outside market hours")
    args = ap.parse_args()
    load_dotenv()

    if not args.force and not market_open(datetime.now(ET)):
        sys.exit("Market is closed (Mon-Fri 9:30-16:00 ET). Use --force to try anyway.")

    while True:
        try:
            take_snapshot()
        except RuntimeError as e:
            print(f"  failed: {e}")
        if not args.every:
            break
        nxt = time.time() + args.every * 60
        if not market_open(datetime.fromtimestamp(nxt, ET)):
            print("Next run would be after the close. Done.")
            break
        print(f"  waiting... next snapshot at {datetime.fromtimestamp(nxt, ET):%H:%M} ET "
              f"(leave this window open; Ctrl+C to stop)", flush=True)
        time.sleep(max(0, nxt - time.time()))


if __name__ == "__main__":
    main()
