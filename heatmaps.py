"""
Build the (time x price) gamma and charm exposure grids for SPX 0DTE
and render them as Plotly heatmaps with a zero-level contour (the "flip").
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from greeks import bs_gamma, bs_charm

MARKET_CLOSE_ET = (16, 0)  # 4:00 PM ET
SECONDS_PER_YEAR = 365.0 * 24 * 3600  # calendar-time convention is fine for 0DTE

def extract_chain_rows(option_data):
    """
    Flatten Schwab option chain into a list of dicts with the fields we need.
    Returns rows with: strike, type ('call'/'put'), oi, iv, expiry_datetime_et
    """
    rows = []
    for side, key in [('call', 'callExpDateMap'), ('put', 'putExpDateMap')]:
        exp_map = option_data.get(key, {}) or {}
        for exp_date_str, strikes in exp_map.items():
            # Schwab format: "2026-04-14:0"  (date:DTE)
            date_part = exp_date_str.split(':')[0]
            # SPX AM-settled expire at open, PM-settled (SPXW 0DTE) at 16:00 ET
            # For 0DTE weekly (SPXW) assume 16:00 ET cash-settle
            exp_dt = datetime.strptime(date_part, '%Y-%m-%d').replace(
                hour=16, minute=0, tzinfo=ZoneInfo("America/New_York")
            )
            for strike_str, contracts in strikes.items():
                c = contracts[0]
                iv = c.get('volatility', None)
                # Schwab returns IV in percent (e.g. 15.3), convert to decimal
                if iv is None or iv <= 0:
                    continue
                iv_dec = iv / 100.0 if iv > 3 else iv
                rows.append({
                    'strike': float(strike_str),
                    'type': side,
                    'oi': c.get('openInterest', 0) or 0,
                    'iv': iv_dec,
                    'expiry': exp_dt,
                })
    return pd.DataFrame(rows)

def build_time_price_grid(spot, chain_df, price_pct=0.01, n_prices=60, n_times=40,
                          r=0.04, metric='gamma'):
    """
    Returns (prices, times_et, Z) where Z[i, j] is dealer exposure at
    price prices[i] and wall-clock time times_et[j].

    metric: 'gamma' or 'charm'
    Dealer convention: dealers short calls (+gamma contribution from calls)
    and long puts (-gamma contribution from puts). This matches the standard
    SqueezeMetrics GEX sign convention and makes "positive = stabilizing".
    """
    if chain_df.empty:
        return None, None, None

    now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    # End of grid: market close on the latest expiry in the chain. We trust
    # whatever date the caller put on chain_df['expiry'] — if they re-stamped
    # it to a past date to show a historical session, honor that.
    end_et = pd.Timestamp(chain_df['expiry'].max()).tz_localize(None) \
        if chain_df['expiry'].max().tzinfo is not None \
        else pd.Timestamp(chain_df['expiry'].max())
    end_et = end_et.to_pydatetime()

    # Start of grid: market open (09:30 ET) on the SAME day as end_et
    session_open = end_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start_et = session_open

    times = pd.date_range(start=start_et, end=end_et, periods=n_times)
    # Strip timezone so Plotly treats these as the same axis as tz-naive candles
    if times.tz is not None:
        times = times.tz_localize(None)
    prices = np.linspace(spot * (1 - price_pct), spot * (1 + price_pct), n_prices)

    # Pre-extract arrays
    K = chain_df['strike'].to_numpy()
    OI = chain_df['oi'].to_numpy()
    IV = chain_df['iv'].to_numpy()
    is_call = (chain_df['type'] == 'call').to_numpy()
    # Strip tz from expiries so they subtract cleanly from tz-naive times
    expiries = pd.to_datetime(chain_df['expiry']).dt.tz_localize(None).to_numpy()

    # Dealer sign: short calls => +, long puts => -
    dealer_sign = np.where(is_call, 1.0, -1.0)

    Z = np.zeros((n_prices, n_times))

    # Min TTE = 5 minutes in years. Prevents gamma from blowing up to billions
    # in the final cells where time-to-expiry approaches zero.
    MIN_TTE_YEARS = (5 * 60) / SECONDS_PER_YEAR

    for j, t in enumerate(times):
        # time to expiry in years for each contract at wall-clock t
        ttes = np.array([
            max((pd.Timestamp(exp).to_pydatetime() - t.to_pydatetime()).total_seconds(), 1.0)
            / SECONDS_PER_YEAR
            for exp in expiries
        ])
        ttes = np.maximum(ttes, MIN_TTE_YEARS)

        for i, S in enumerate(prices):
            if metric == 'gamma':
                vals = bs_gamma(S, K, ttes, IV, r=r)
                # GEX per contract: gamma * OI * 100 * S  (dollar gamma)
                contrib = dealer_sign * vals * OI * 100.0 * S
            else:  # charm
                call_c = bs_charm(S, K, ttes, IV, r=r, option_type='call')
                put_c = bs_charm(S, K, ttes, IV, r=r, option_type='put')
                vals = np.where(is_call, call_c, put_c)
                # Charm exposure: charm * OI * 100 (delta change per year)
                # divide by 252 to get "per trading day" for readable numbers
                contrib = dealer_sign * vals * OI * 100.0 / 252.0

            Z[i, j] = np.nansum(contrib)

    return prices, times, Z

def plot_heatmap(prices, times, Z, spot, title, colorscale='RdBu', metric_name='Gamma'):
    """Render a time-price heatmap with a zero contour (flip line).
    Uses x0/dx for the time axis so Plotly treats it as a continuous date
    axis (not category). This is what lets the price line overlay align.

    metric_name: 'Gamma' or 'Charm' — used for legend labels.
    """
    if prices is None:
        return go.Figure()

    vmax = np.nanpercentile(np.abs(Z), 90)
    if vmax == 0 or np.isnan(vmax):
        vmax = 1.0

    # x0 = first time as a pandas Timestamp → Plotly date axis
    # dx = step in MILLISECONDS (Plotly's unit for date axes)
    t0 = pd.Timestamp(times[0])
    if len(times) > 1:
        dt_ms = (pd.Timestamp(times[1]) - t0).total_seconds() * 1000.0
    else:
        dt_ms = 60_000.0  # fallback 1 minute

    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        x0=t0.isoformat(),
        dx=dt_ms,
        y=prices,
        z=Z,
        colorscale=colorscale,
        zmid=0,
        zmin=-vmax,
        zmax=vmax,
        colorbar=dict(title=title.split()[0]),
        hovertemplate='Time: %{x}<br>Price: %{y:.2f}<br>Value: %{z:,.0f}<extra></extra>',
        showlegend=False,
    ))

    fig.add_trace(go.Contour(
        x0=t0.isoformat(),
        dx=dt_ms,
        y=prices,
        z=Z,
        showscale=False,
        contours=dict(start=0, end=0, size=1, coloring='none', showlines=True),
        line=dict(color='#f5a524', width=2, dash='dot'),
        hoverinfo='skip',
        showlegend=False,
    ))

    fig.add_hline(y=spot, line_color='#f5a524', line_width=1.5,
                  annotation_text=f'SPOT {spot:,.2f}', annotation_position='top right',
                  annotation_font=dict(color='#f5a524', family='IBM Plex Mono, monospace', size=11))

    # Proxy legend entries — invisible traces whose only purpose is to
    # populate the legend. Plotly's heatmap and contour don't show useful
    # legend entries on their own, and add_hline doesn't either.
    # Use mode='lines' for the line-style entries so we can actually use 'dash'.
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='markers',
        marker=dict(color='#2fbf71' if metric_name == 'Gamma' else '#60a5fa',
                    size=14, symbol='square'),
        name=f'{metric_name} (heatmap)',
        showlegend=True,
        hoverinfo='skip',
    ))
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='lines',
        line=dict(color='#f5a524', width=2, dash='dot'),
        name=f'{metric_name} Zero',
        showlegend=True,
        hoverinfo='skip',
    ))
    fig.add_trace(go.Scatter(
        x=[None], y=[None],
        mode='lines',
        line=dict(color='#f5a524', width=2),
        name=f'Spot ({spot:.2f})',
        showlegend=True,
        hoverinfo='skip',
    ))

    fig.update_layout(
        title=title,
        xaxis_title='Time (ET)',
        yaxis_title='SPX Price',
        height=650,  # +50px to make room for legend at bottom
        uirevision='constant',
        xaxis=dict(type='date'),
        legend=dict(
            orientation='h',
            yanchor='top',
            y=-0.15,
            xanchor='center',
            x=0.5,
            bgcolor='rgba(0,0,0,0)',
            bordercolor='rgba(255,255,255,0.2)',
            borderwidth=1,
        ),
        margin=dict(l=70, r=20, t=50, b=80),  # extra bottom margin so legend has room
        paper_bgcolor='#0b0c0e', plot_bgcolor='#0b0c0e',
        font=dict(family='IBM Plex Mono, monospace', size=11, color='#8b9098'),
        title_font=dict(family='IBM Plex Sans, sans-serif', size=13, color='#e6e7e9'),
    )
    fig.update_xaxes(gridcolor='#24272c', linecolor='#24272c')
    fig.update_yaxes(gridcolor='#24272c', linecolor='#24272c', tickformat=',.0f')
    return fig

def add_price_line_to_heatmap(fig, price_df, session_start=None, session_end=None):
    """Overlay SPX price as a white line on top of the heatmap.

    Using go.Scatter (a line trace) instead of go.Candlestick avoids all the
    axis-type headaches that come with mixing Candlestick + Heatmap. A Scatter
    trace overlays cleanly on the heatmap's date axis with no special handling.

    price_df: DataFrame with columns 'datetime', 'close' (and optionally OHLC)
    """
    if price_df is None or price_df.empty:
        if session_start is not None and session_end is not None:
            fig.update_xaxes(range=[pd.Timestamp(session_start).isoformat(),
                                    pd.Timestamp(session_end).isoformat()])
        return fig

    df = price_df.copy()
    if session_start is not None and session_end is not None:
        df = df[(df['datetime'] >= session_start) & (df['datetime'] <= session_end)]

    if df.empty:
        return fig

    x_vals = [pd.Timestamp(t).isoformat() for t in df['datetime']]

    # Main price line
    fig.add_trace(go.Scatter(
        x=x_vals,
        y=df['close'].tolist(),
        mode='lines',
        line=dict(color='white', width=2),
        name='SPX Price',
        showlegend=True,
        hovertemplate='%{x}<br>SPX: %{y:.2f}<extra></extra>',
    ))

    # Subtle high/low envelope (faint shadow) so you can still see intraday range
    if 'high' in df.columns and 'low' in df.columns:
        fig.add_trace(go.Scatter(
            x=x_vals + x_vals[::-1],
            y=df['high'].tolist() + df['low'].tolist()[::-1],
            fill='toself',
            fillcolor='rgba(255,255,255,0.15)',
            line=dict(width=0),
            hoverinfo='skip',
            showlegend=True,
            name='SPX Range (H/L)',
        ))

    if session_start is not None and session_end is not None:
        fig.update_xaxes(range=[pd.Timestamp(session_start).isoformat(),
                                pd.Timestamp(session_end).isoformat()])
    return fig