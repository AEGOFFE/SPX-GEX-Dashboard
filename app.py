"""
SPX 0DTE Gamma Exposure dashboard (Streamlit).

Two modes:
  LIVE - pulls the current chain from the Schwab API (needs .env credentials; local use only).
  DEMO - renders a saved snapshot from snapshots/ (no credentials, no login code loaded).
Demo mode is used automatically when no APP_KEY is configured, or when DEMO_MODE=1.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from gex import compute_results, fmt_usd
from heatmaps import plot_heatmap, add_price_line_to_heatmap

load_dotenv()
ET = ZoneInfo("America/New_York")
DEMO_MODE = os.getenv("DEMO_MODE", "").lower() in ("1", "true", "yes") or not os.getenv("APP_KEY")

# ---- Terminal palette (green/red = gamma sign only, amber = key levels) ----
BG, GRID, TEXT, MUTED = '#0b0c0e', '#24272c', '#e6e7e9', '#8b9098'
POS, NEG, AMBER, ACCENT = '#2fbf71', '#e5484d', '#f5a524', '#22d3ee'
MONO = "IBM Plex Mono, SFMono-Regular, Consolas, monospace"

def style_fig(fig, height):
    """Shared dark 'terminal' styling for every Plotly chart."""
    fig.update_layout(
        height=height, paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(family=MONO, size=11, color=MUTED),
        margin=dict(l=60, r=20, t=40, b=40),
        hoverlabel=dict(bgcolor='#131519', bordercolor=GRID, font=dict(family=MONO, color=TEXT)),
        uirevision='constant',
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zerolinecolor='#3a3e45', linecolor=GRID, tickfont=dict(color=MUTED))
    return fig

def find_walls(agg):
    """Call wall = strike with the largest positive net GEX; put wall = most negative."""
    call_wall = agg.loc[agg['GEX'].idxmax()] if (agg['GEX'] > 0).any() else None
    put_wall = agg.loc[agg['GEX'].idxmin()] if (agg['GEX'] < 0).any() else None
    return call_wall, put_wall

def focus_range(df, spot_price, gex_flip, pad=15):
    """X-range covering strikes that carry real exposure (>=1% of the largest bar), plus spot/flip."""
    peak = df['GEX'].abs().max()
    active = df[df['GEX'].abs() >= 0.01 * peak]['Strike'] if peak > 0 else df['Strike']
    pts = list(active) + [spot_price] + ([gex_flip] if gex_flip else [])
    return min(pts) - pad, max(pts) + pad

def plot_gex(df, spot_price, gex_flip, call_wall=None, put_wall=None):
    """Net GEX by strike, zoomed to where exposure actually is, with key levels labelled."""
    lo, hi = focus_range(df, spot_price, gex_flip)
    df = df[(df['Strike'] >= lo) & (df['Strike'] <= hi)]
    fig = go.Figure(go.Bar(
        x=df['Strike'], y=df['GEX'],
        marker_color=[POS if v >= 0 else NEG for v in df['GEX']], marker_line_width=0,
        name='Net GEX',
        customdata=[fmt_usd(v) for v in df['GEX']],
        hovertemplate='Strike %{x:,.0f}<br>Net GEX %{customdata} per 1%<extra></extra>',
    ))

    # Key levels: spot = solid amber, flip = dashed amber. Labels sit ABOVE the plot and point
    # away from each other so they never collide with each other or the axis ticks.
    flip_right = gex_flip is not None and gex_flip >= spot_price
    fig.add_vline(x=spot_price, line_color=AMBER, line_width=1.5)
    fig.add_annotation(x=spot_price, y=1, yref='paper', yanchor='bottom', showarrow=False,
                       xanchor='right' if flip_right else 'left', xshift=-4 if flip_right else 4,
                       text=f"SPOT {spot_price:,.2f}", font=dict(color=AMBER, family=MONO, size=11))
    if gex_flip is not None:
        fig.add_vline(x=gex_flip, line_color=AMBER, line_width=1.5, line_dash='dash')
        fig.add_annotation(x=gex_flip, y=1, yref='paper', yanchor='bottom', showarrow=False,
                           xanchor='left' if flip_right else 'right', xshift=4 if flip_right else -4,
                           text=f"FLIP {gex_flip:,.2f}", font=dict(color=AMBER, family=MONO, size=11))

    # Walls: label the tip of the largest call and put bars.
    for wall, name, color in ((call_wall, 'CALL WALL', POS), (put_wall, 'PUT WALL', NEG)):
        if wall is not None:
            # label sits beside the bar tip, pointing toward the chart centre so it is never clipped
            side = -1 if wall['Strike'] > (lo + hi) / 2 else 1
            fig.add_annotation(x=wall['Strike'], y=wall['GEX'], showarrow=True, arrowhead=0,
                               arrowcolor=color, ax=side * 40, ay=-16 if wall['GEX'] > 0 else -12,
                               xanchor='left' if side > 0 else 'right',
                               text=f"{name} {wall['Strike']:,.0f} · {fmt_usd(wall['GEX'])}",
                               font=dict(color=color, family=MONO, size=11),
                               bgcolor=BG)

    style_fig(fig, 560)
    fig.update_layout(showlegend=False, bargap=0.15, hovermode='x unified',
                      margin=dict(l=70, r=20, t=40, b=50))
    fig.update_xaxes(range=[lo, hi], title_text='STRIKE', tickformat=',.0f')
    # $B/$M tick labels (Plotly's SI format would print "G" for billions)
    ymin, ymax = min(df['GEX'].min(), 0), max(df['GEX'].max(), 0)
    pad = 0.08 * ((ymax - ymin) or 1)
    raw = ((ymax - ymin) or 1) / 6
    mag = 10 ** np.floor(np.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks = np.arange(np.floor((ymin - pad) / step) * step, ymax + pad + step, step)
    fig.update_yaxes(title_text='NET GEX ($ PER 1% MOVE)', range=[ymin - pad, ymax + pad],
                     tickvals=list(ticks), ticktext=[fmt_usd(t) if abs(t) > 1e-9 else '$0' for t in ticks])
    return fig


# ======================================================================
# Page chrome
# ======================================================================
st.set_page_config(page_title="SPX 0DTE Gamma Exposure", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; }
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }
.block-container { padding-top: 1rem; padding-bottom: 1rem; max-width: 100%; }
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace; font-size: 1.45rem; font-variant-numeric: tabular-nums; }
[data-testid="stMetricLabel"] p { font-size: 0.68rem; letter-spacing: 0.1em; text-transform: uppercase; color: #8b9098; }
[data-testid="stMetricDelta"] { font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; }
[data-testid="stHorizontalBlock"] [data-testid="stMetric"] { border-left: 1px solid #24272c; padding-left: 0.75rem; }
.gex-header { font-size: 0.8rem; font-weight: 600; letter-spacing: 0.12em; color: #e6e7e9; }
.gex-status { font-family: 'IBM Plex Mono', monospace; font-size: 0.72rem; color: #8b9098; }
.gex-regime { font-family: 'IBM Plex Mono', monospace; font-size: 0.78rem; color: #e6e7e9; padding: 0.35rem 0; border-top: 1px solid #24272c; border-bottom: 1px solid #24272c; }
</style>
""", unsafe_allow_html=True)


def header(status, show_refresh=False):
    """One compact row: title + data status; optional refresh control (live only)."""
    left, right = st.columns([8, 1])
    with left:
        st.markdown(f'<span class="gex-header">SPX 0DTE · GAMMA EXPOSURE</span>&nbsp;&nbsp;'
                    f'<span class="gex-status">{status}</span>', unsafe_allow_html=True)
    if show_refresh:
        with right:
            if st.button("Refresh", width="stretch"):
                st.rerun()


def footer():
    st.markdown('<div class="gex-status" style="margin-top:1rem;border-top:1px solid #24272c;padding-top:.5rem">'
                'GEX = gamma x OI x 100 x spot^2 x 1% · flip = zero-gamma level (Black-Scholes sweep, +/-3%) · '
                'dealers assumed short calls / long puts · educational portfolio project, not investment advice'
                '</div>', unsafe_allow_html=True)


# ======================================================================
# Dashboard (identical for live and demo data)
# ======================================================================
def render_dashboard(res, candles_debug, demo):
    """Draw everything from a results dict (gex.compute_results live, or a demo snapshot)."""
    spot_price, gex_flip, agg = res['spot'], res['flip'], res['strikes']
    net_gex = res['call_gex'] + res['put_gex']
    call_wall, put_wall = find_walls(agg)

    # Key levels strip: spot and flip first, then walls, then totals
    m = st.columns([1.2, 1.2, 1, 1, 1, 1, 1])
    m[0].metric("SPX Spot", f"{spot_price:,.2f}")
    if gex_flip is not None:
        m[1].metric("Zero-Gamma Flip", f"{gex_flip:,.2f}",
                    f"{(gex_flip / spot_price - 1) * 100:+.2f}% vs spot", delta_color="off")
    else:
        m[1].metric("Zero-Gamma Flip", "N/A", "no crossing / chain expired", delta_color="off")
    m[2].metric("Call Wall", f"{call_wall['Strike']:,.0f}" if call_wall is not None else "N/A",
                fmt_usd(call_wall['GEX']) if call_wall is not None else None, delta_color="off")
    m[3].metric("Put Wall", f"{put_wall['Strike']:,.0f}" if put_wall is not None else "N/A",
                fmt_usd(put_wall['GEX']) if put_wall is not None else None, delta_color="off")
    m[4].metric("Net GEX / 1%", fmt_usd(net_gex))
    m[5].metric("Call GEX / 1%", fmt_usd(res['call_gex']))
    m[6].metric("Put GEX / 1%", fmt_usd(res['put_gex']))

    if gex_flip is not None:
        if spot_price >= gex_flip:
            regime = ("POSITIVE GAMMA", POS, "spot above flip: dealer hedging tends to dampen moves")
        else:
            regime = ("NEGATIVE GAMMA", NEG, "spot below flip: dealer hedging tends to amplify moves")
        st.markdown(f'<div class="gex-regime">REGIME&nbsp;&nbsp;<b style="color:{regime[1]}">'
                    f'{regime[0]}</b>&nbsp;&nbsp;·&nbsp;&nbsp;{regime[2]}</div>', unsafe_allow_html=True)

    tab1, tab2, tab3 = st.tabs(["GEX BY STRIKE", "GAMMA SURFACE", "CHARM SURFACE"])
    with tab1:
        st.plotly_chart(plot_gex(agg, spot_price, gex_flip, call_wall, put_wall),
                        width="stretch", key='gex_chart')

    price_date, candles_df = res['price_date'], res['candles']
    session_start = datetime.combine(price_date, datetime.min.time()).replace(hour=9, minute=30)
    session_end = datetime.combine(price_date, datetime.min.time()).replace(hour=16, minute=0)
    if candles_df is None:
        st.caption(f"Intraday price path unavailable ({candles_debug}).")
    elif not demo and price_date != res['as_of'].date():
        st.info(f"Showing the **{price_date}** session (today's intraday data not yet available from Schwab). "
                f"Heatmap reflects current chain positioning projected onto that day.")
    else:
        st.caption(f"{price_date} session · {candles_debug}")

    surface_specs = {
        'gamma': (tab2, 'Dealer Gamma Exposure ($ / pt)', [[0, NEG], [0.5, BG], [1, POS]], 'Gamma',
                  "Green = positive (stabilizing) gamma · Red = negative (destabilizing). "
                  "Amber dotted curve = zero-gamma line · white line = SPX path."),
        'charm': (tab3, 'Dealer Charm Exposure (Δ / day)', [[0, '#c084fc'], [0.5, BG], [1, '#60a5fa']], 'Charm',
                  "Blue = supportive charm (dealer hedging buys dips) · "
                  "Purple = destabilizing charm. Amber dotted curve = charm zero line."),
    }
    for name, (tab, title, scale, label, caption) in surface_specs.items():
        with tab:
            surf = res['surfaces'].get(name)
            if surf is None:
                st.caption("Surface unavailable (no contracts with implied volatility near spot).")
                continue
            prices, times, Z = surf
            fig = plot_heatmap(prices, times, Z, spot_price, title, colorscale=scale, metric_name=label)
            fig = add_price_line_to_heatmap(fig, candles_df, session_start, session_end)
            st.plotly_chart(fig, width="stretch", key=f'{name}_hm')
            st.caption(caption)

    st.markdown('<div class="gex-header" style="margin-top:1rem">GEX BY STRIKE · TABLE</div>', unsafe_allow_html=True)
    table = pd.DataFrame({'Strike': agg['Strike'], 'Net GEX ($M / 1%)': (agg['GEX'] / 1e6).round(1)})
    st.dataframe(table, width="stretch", height=400, hide_index=True,
                 column_config={'Strike': st.column_config.NumberColumn(format="%.0f"),
                                'Net GEX ($M / 1%)': st.column_config.NumberColumn(format="%.1f")})


# ======================================================================
# DEMO mode: saved snapshot, no credentials, no login code
# ======================================================================
if DEMO_MODE:
    from demo_data import load_snapshot
    snap = load_snapshot()
    if snap is None:
        header("DEMO · NO SNAPSHOT FOUND")
        st.warning("No snapshot in snapshots/. Run `python snapshot.py` during market hours to create one.")
        st.stop()
    as_of = snap["as_of"]
    if snap["sample"]:
        header(f"SAMPLE · SYNTHETIC DATA FOR PREVIEWING THE LAYOUT · NOT REAL MARKET DATA")
    else:
        header(f"SNAPSHOT · {as_of:%a %d %b %Y · %H:%M} ET · NOT LIVE · calculated from real market data, frozen for this demo")
    render_dashboard(snap, snap["candles_source"], demo=True)
    footer()

# ======================================================================
# LIVE mode: Schwab API (local use with your own credentials)
# ======================================================================
else:
    from streamlit_autorefresh import st_autorefresh
    from schwab_live import (TOKEN_FILE, get_auth_url, get_tokens, get_valid_token,
                             get_spx_price, get_option_chain, get_spx_candles)

    # Auto-refresh: 30 s in the last 15 minutes before the 4:00 PM ET close, else 120 s
    now_t = datetime.now(ET).time()
    refresh_interval_ms = 30000 if datetime.strptime("15:45", "%H:%M").time() <= now_t < \
        datetime.strptime("16:00", "%H:%M").time() else 120000
    st_autorefresh(interval=refresh_interval_ms, key="data_refresh")

    access_token = get_valid_token()
    if not access_token:
        header("LIVE · NOT CONNECTED")
        st.warning("Schwab login required")
        st.markdown(f"**Step 1.** [Authorize with Schwab]({get_auth_url()}) and log in.")
        st.markdown("**Step 2.** When the browser lands on the blank/\"can't connect\" page, copy the **whole address** "
                    "from the address bar and paste it below. No editing needed.")
        with st.form("schwab_login", clear_on_submit=True):
            pasted = st.text_input("Callback URL (or just the code)", type="password",
                                   placeholder="https://127.0.0.1/?code=...&session=...")
            submitted = st.form_submit_button("Log in")   # Enter also submits
        if submitted:
            if pasted:
                with st.spinner("Exchanging code for tokens..."):
                    if get_tokens(pasted):
                        st.success("Logged in. Loading data...")
                        st.rerun()
            else:
                st.error("Paste the URL first")
    else:
        header(f"LIVE · SCHWAB MARKET DATA · UPDATED {datetime.now(ET):%H:%M:%S} ET · "
               f"AUTO-REFRESH {refresh_interval_ms // 1000}s", show_refresh=True)
        with st.spinner("Fetching SPX data..."):
            spot_price = get_spx_price(access_token)
            option_data = get_option_chain(access_token) if spot_price else None
        if not spot_price:
            st.error("Failed to fetch SPX price")
        elif not option_data:
            st.error("Failed to fetch option chain")
        else:
            candles_df, candles_debug = get_spx_candles(access_token, minute_freq=10, spx_spot=spot_price)
            with st.spinner("Computing exposure and surfaces..."):
                res = compute_results(option_data, spot_price, datetime.now(ET), candles_df)
            if res is None:
                st.warning("No 0DTE contracts in the chain (weekend, holiday, or after expiry).")
            else:
                render_dashboard(res, candles_debug, demo=False)
        footer()
        if st.button("Log out"):
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            st.rerun()
