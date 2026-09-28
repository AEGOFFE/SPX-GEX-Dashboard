"""
Live Schwab API access: OAuth tokens, SPX quote, 0DTE option chain, intraday bars.

Only imported when the app runs in LIVE mode. The public demo never loads this module,
so a deployed demo has no login flow, no token handling and no credentials to leak.
"""
import base64
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs, unquote

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

APP_KEY = os.getenv('APP_KEY')
APP_SECRET = os.getenv('APP_SECRET')
CALLBACK_URL = os.getenv('CALLBACK_URL')

# Token file lives OUTSIDE the repo; path comes from .env (SCHWAB_TOKEN_FILE)
TOKEN_FILE = os.getenv('SCHWAB_TOKEN_FILE')
if not TOKEN_FILE:
    raise RuntimeError("SCHWAB_TOKEN_FILE is not set. Add it to your .env (see .env.example).")

# Schwab API endpoints
AUTH_URL = 'https://api.schwabapi.com/v1/oauth/authorize'
TOKEN_URL = 'https://api.schwabapi.com/v1/oauth/token'
OPTION_CHAIN_URL = 'https://api.schwabapi.com/marketdata/v1/chains'
QUOTE_URL = 'https://api.schwabapi.com/marketdata/v1/quotes'
PRICE_HISTORY_URL = 'https://api.schwabapi.com/marketdata/v1/pricehistory'

def get_auth_url():
    """Generate the authorization URL"""
    params = {
        'client_id': APP_KEY,
        'redirect_uri': CALLBACK_URL,
        'response_type': 'code'
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def extract_auth_code(pasted):
    """
    Accept whatever the user pastes after logging in to Schwab and return the decoded code:
      - the full callback URL  (https://127.0.0.1/?code=C0.abc...%40&session=...)
      - just the code, still URL-encoded  (C0.abc...%40)
      - just the code, already decoded   (C0.abc...@)
    Schwab URL-encodes the trailing '@' as '%40'; parse_qs/unquote turn it back into '@'.
    """
    text = (pasted or "").strip().strip('"').strip("'")
    if not text:
        return None
    if "code=" in text:
        query = urlparse(text).query or text.split("?", 1)[-1]
        codes = parse_qs(query).get("code")
        return codes[0].strip() if codes else None
    return unquote(text)


def get_tokens(auth_code):
    """Exchange authorization code (or the full pasted callback URL) for tokens"""
    auth_code = extract_auth_code(auth_code)
    if not auth_code:
        st.error("Couldn't find a code in what you pasted. Paste the full address from the browser bar.")
        return None
    credentials = f"{APP_KEY}:{APP_SECRET}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()
    
    headers = {
        'Authorization': f'Basic {encoded_credentials}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    data = {
        'grant_type': 'authorization_code',
        'code': auth_code,
        'redirect_uri': CALLBACK_URL
    }
    
    response = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    
    if response.status_code == 200:
        tokens = response.json()
        save_tokens(tokens)
        return tokens
    else:
        print(f"[auth] token exchange failed: {response.status_code} - {response.text[:300]}")
        if response.status_code in (400, 401):
            st.error("Schwab rejected the code. Codes expire within a minute or so and work only once. "
                     "Click **Authorize with Schwab** again and paste the new URL right away.")
        else:
            st.error(f"Token exchange failed (HTTP {response.status_code}). See console for details.")
        return None

def refresh_access_token():
    """Refresh the access token using refresh token"""
    tokens = load_tokens()
    if not tokens or 'refresh_token' not in tokens:
        return None

    credentials = f"{APP_KEY}:{APP_SECRET}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        'Authorization': f'Basic {encoded_credentials}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    data = {
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token']
    }

    response = requests.post(TOKEN_URL, headers=headers, data=data)

    if response.status_code == 200:
        new_tokens = response.json()

        if 'refresh_token' not in new_tokens and 'refresh_token' in tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']

        save_tokens(new_tokens)
        return new_tokens
    else:
        return None

def save_tokens(tokens):
    """Save tokens to file."""
    tokens['timestamp'] = datetime.now().isoformat()

    expires_in = tokens.get("expires_in")
    if expires_in is not None:
        tokens["expires_at"] = datetime.now().timestamp() + int(expires_in) - 60

    with open(TOKEN_FILE, 'w') as f:
        json.dump(tokens, f)

def load_tokens():
    """Load tokens from file"""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return json.load(f)
    return None

def get_valid_token():
    """Get a valid access token, refreshing if necessary."""
    tokens = load_tokens()

    if not tokens:
        return None

    # Preferred format: expires_at (used by quant tool)
    expires_at = tokens.get("expires_at")
    if expires_at is not None:
        if expires_at > datetime.now().timestamp():
            return tokens.get("access_token")
        else:
            tokens = refresh_access_token()
            return tokens.get("access_token") if tokens else None

    # Fallback format: timestamp (older GEX format)
    timestamp_str = tokens.get("timestamp")
    if timestamp_str:
        timestamp = datetime.fromisoformat(timestamp_str)
        if datetime.now() - timestamp > timedelta(minutes=25):
            tokens = refresh_access_token()
        return tokens.get("access_token") if tokens else None

    return None

def get_spx_price(access_token):
    """Get current SPX price"""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    params = {
        'symbols': '$SPX',
        'fields': 'quote'
    }
    
    response = requests.get(QUOTE_URL, headers=headers, params=params)
    
    if response.status_code == 200:
        data = response.json()
        return data['$SPX']['quote']['lastPrice']
    return None

def get_spx_candles(access_token, minute_freq=10, spx_spot=None):
    """
    Fetch today's intraday price path. Uses SPY because Schwab's pricehistory
    for $SPX cash index only updates end-of-day, while SPY updates live.
    SPY is scaled to SPX-equivalent using spx_spot/last_spy_close ratio.
    Forces today's session by passing explicit startDate (midnight ET today).
    """
    headers = {'Authorization': f'Bearer {access_token}'}
    symbols_to_try = ['SPY', '$SPX.X', '$SPX', 'SPX']
    last_err = None

    # startDate = midnight ET today, in epoch millis (UTC)
    from zoneinfo import ZoneInfo as _ZI
    today_midnight_et = datetime.now(_ZI("America/New_York")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_ms = int(today_midnight_et.timestamp() * 1000)
    end_ms = int(datetime.now().timestamp() * 1000)

    for sym in symbols_to_try:
        params = {
            'symbol': sym,
            'periodType': 'day',
            'frequencyType': 'minute',
            'frequency': minute_freq,
            'startDate': start_ms,
            'endDate': end_ms,
            'needExtendedHoursData': 'false',
        }
        try:
            r = requests.get(PRICE_HISTORY_URL, headers=headers, params=params, timeout=10)
            if r.status_code != 200:
                last_err = f"{sym}: HTTP {r.status_code} — {r.text[:150]}"
                continue
            payload = r.json()
            candles = payload.get('candles', [])
            if not candles:
                last_err = f"{sym}: empty (today's bars not yet available)"
                continue

            df = pd.DataFrame(candles)
            df_utc = pd.to_datetime(df['datetime'], unit='ms', utc=True).dt.tz_convert('America/New_York').dt.tz_localize(None)
            hours_utc = df_utc.dt.hour
            utc_in_session = ((hours_utc >= 9) & (hours_utc < 16)).mean()

            if utc_in_session > 0.8:
                df['datetime'] = df_utc
                interp = 'UTC'
            else:
                df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
                interp = 'naive-ET'

            # Verify the bars are actually from today; if Schwab ignored our
            # startDate and returned an older session, skip and try next symbol
            bar_date = df['datetime'].iloc[-1].date()
            today_date = today_midnight_et.date()
            if bar_date != today_date:
                last_err = f"{sym}: returned {bar_date} not today ({today_date})"
                continue

            # Scale SPY → SPX-equivalent using the live spot/last-close ratio
            if sym == 'SPY':
                last_spy_close = df['close'].iloc[-1]
                if spx_spot is not None and last_spy_close > 0:
                    scalar = spx_spot / last_spy_close
                else:
                    scalar = 10.0
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col] * scalar

            first = df['datetime'].iloc[0]
            last = df['datetime'].iloc[-1]
            return df[['datetime', 'open', 'high', 'low', 'close']], \
                f"{sym} · {len(df)} bars · {interp} · {first.strftime('%Y-%m-%d %H:%M')}→{last.strftime('%H:%M')}"
        except Exception as e:
            last_err = f"{sym}: exception {e}"
            continue

    return None, f"All symbols failed. Last error: {last_err}"

def get_option_chain(access_token):
    """Fetch 0DTE option chain for SPX"""
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    # Get today's date for 0DTE
    today = datetime.now().strftime('%Y-%m-%d')
    
    params = {
        'symbol': '$SPX',
        'contractType': 'ALL',
        'strategy': 'SINGLE',
        'range': 'ALL',
        'fromDate': today,
        'toDate': today
    }
    
    response = requests.get(OPTION_CHAIN_URL, headers=headers, params=params)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"[chain] fetch failed: {response.status_code} - {response.text[:300]}")
        st.error(f"Failed to fetch option chain (HTTP {response.status_code}). See console for details.")
        return None

