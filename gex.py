"""
GEX math: per-contract gamma exposure, aggregation by strike, and the zero-gamma flip.
Pure functions - no UI, no network - so live and demo mode share exactly the same numbers.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from heatmaps import extract_chain_rows


def calculate_gex(option_data, spot_price):
    """Calculate GEX for each strike"""
    gex_data = []
    
    # Process calls (positive GEX)
    if 'callExpDateMap' in option_data:
        for exp_date, strikes in option_data['callExpDateMap'].items():
            for strike, contracts in strikes.items():
                contract = contracts[0]  # First contract at this strike
                strike_price = float(strike)
                gamma = contract.get('gamma', 0)
                oi = contract.get('openInterest', 0)
                
                # GEX ($ per 1% move) = Gamma * OI * 100 * Spot^2 * 0.01
                gex = gamma * oi * 100 * spot_price * spot_price * 0.01
                
                gex_data.append({
                    'Strike': strike_price,
                    'Type': 'Call',
                    'GEX': gex,
                    'Gamma': gamma,
                    'OI': oi
                })
    
    # Process puts (negative GEX)
    if 'putExpDateMap' in option_data:
        for exp_date, strikes in option_data['putExpDateMap'].items():
            for strike, contracts in strikes.items():
                contract = contracts[0]
                strike_price = float(strike)
                gamma = contract.get('gamma', 0)
                oi = contract.get('openInterest', 0)
                
                # Puts are negative GEX
                gex = -1 * gamma * oi * 100 * spot_price * spot_price * 0.01
                
                gex_data.append({
                    'Strike': strike_price,
                    'Type': 'Put',
                    'GEX': gex,
                    'Gamma': gamma,
                    'OI': oi
                })
    
    return pd.DataFrame(gex_data)

def aggregate_gex(df):
    """Aggregate GEX by strike"""
    return df.groupby('Strike').agg({
        'GEX': 'sum'
    }).reset_index().sort_values('Strike')

def fmt_usd(x):
    """Format dollar GEX compactly, e.g. $2.41B / -$830.2M."""
    sign = '-' if x < 0 else ''
    x = abs(x)
    for div, suf in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if x >= div:
            return f"{sign}${x/div:,.2f}{suf}"
    return f"{sign}${x:,.0f}"

def find_gex_flip(option_data, spot_price, pct_range=0.03, steps=301, r=0.04, as_of=None):
    """
    Zero-gamma level: sweep hypothetical SPX prices, re-price every contract's
    gamma with Black-Scholes (its own IV, current time to expiry), sum dealer
    GEX (calls +, puts -), and interpolate where the total crosses zero.
    Returns the crossing closest to spot, or None (e.g. chain expired).
    """
    import numpy as np
    from greeks import bs_gamma
    rows = extract_chain_rows(option_data)
    if rows.empty:
        return None
    now_et = as_of or datetime.now(ZoneInfo("America/New_York"))  # demo passes snapshot time
    expiry = rows['expiry'].max()
    if now_et >= expiry:
        return None  # 0DTE chain already expired -> no meaningful flip
    tte = max((expiry - now_et).total_seconds(), 300) / (365.0 * 24 * 3600)
    K = rows['strike'].to_numpy(); OI = rows['oi'].to_numpy(); IV = rows['iv'].to_numpy()
    sign = np.where(rows['type'].to_numpy() == 'call', 1.0, -1.0)
    prices = np.linspace(spot_price * (1 - pct_range), spot_price * (1 + pct_range), steps)
    totals = np.array([np.nansum(sign * bs_gamma(S, K, tte, IV, r=r) * OI * 100 * S * S * 0.01)
                       for S in prices])
    crossings = []
    for i in range(len(prices) - 1):
        a, b = totals[i], totals[i + 1]
        if a == 0:
            crossings.append(prices[i])
        elif a * b < 0:
            crossings.append(prices[i] + (prices[i + 1] - prices[i]) * (-a) / (b - a))
    if not crossings:
        return None
    return float(min(crossings, key=lambda x: abs(x - spot_price)))


# ----------------------------------------------------------------------
# One entry point for every number the dashboard shows. Used by the live app AND by
# snapshot.py, so a saved snapshot contains exactly what the live screen would show -
# and ONLY those calculated outputs, never the raw option chain.
# ----------------------------------------------------------------------
def clean_chain(option_data):
    """Replace missing / NaN / Schwab placeholder (-999) greeks with 0 so the math never breaks."""
    import math
    for key in ('callExpDateMap', 'putExpDateMap'):
        for strikes in (option_data.get(key) or {}).values():
            for contracts in strikes.values():
                for c in contracts:
                    for f in ('gamma', 'volatility'):
                        try:
                            v = float(c.get(f))
                        except (TypeError, ValueError):
                            v = 0.0
                        c[f] = 0.0 if (math.isnan(v) or v <= -999) else v
                    c['openInterest'] = int(c.get('openInterest') or 0)
    return option_data


def compute_results(option_data, spot_price, as_of, candles_df=None):
    """
    Returns a dict with everything the dashboard renders:
      spot, as_of, n_contracts, call_gex, put_gex, net_gex, flip,
      strikes (DataFrame Strike/GEX within +/-5% of spot),
      surfaces {'gamma'|'charm': (prices, times, Z) or None},
      price_date, candles (DataFrame or None)
    Returns None when the chain has no contracts.
    """
    from heatmaps import build_time_price_grid
    option_data = clean_chain(option_data)
    gex_df = calculate_gex(option_data, spot_price)
    if gex_df.empty:
        return None
    agg = aggregate_gex(gex_df)
    strikes = agg[(agg['Strike'] >= spot_price * 0.95) & (agg['Strike'] <= spot_price * 1.05)].reset_index(drop=True)

    # Heatmaps: strikes within +/-1.5% of spot; the session is the date of the price data
    if candles_df is not None and not candles_df.empty:
        price_date = candles_df['datetime'].iloc[-1].date()
    else:
        price_date = as_of.date()
    chain_df = extract_chain_rows(option_data)
    surfaces = {'gamma': None, 'charm': None}
    if not chain_df.empty:
        chain_df = chain_df[(chain_df['strike'] >= spot_price * 0.985) & (chain_df['strike'] <= spot_price * 1.015)].copy()
        chain_df['expiry'] = chain_df['expiry'].apply(
            lambda dt: dt.replace(year=price_date.year, month=price_date.month, day=price_date.day))
        for metric in ('gamma', 'charm'):
            prices, times, Z = build_time_price_grid(spot_price, chain_df, price_pct=0.01,
                                                     n_prices=60, n_times=40, metric=metric)
            surfaces[metric] = None if prices is None else (prices, times, Z)

    return {
        'spot': float(spot_price),
        'as_of': as_of,
        'n_contracts': int(len(gex_df)),
        'call_gex': float(gex_df[gex_df['Type'] == 'Call']['GEX'].sum()),
        'put_gex': float(gex_df[gex_df['Type'] == 'Put']['GEX'].sum()),
        'flip': find_gex_flip(option_data, spot_price, as_of=as_of),
        'strikes': strikes,
        'surfaces': surfaces,
        'price_date': price_date,
        'candles': candles_df,
    }
