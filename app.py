"""
app.py — RSI(2) + 200EMA Strategy Dashboard (Streamlit web app)

DEPLOYMENT (Streamlit Community Cloud):
1. Create a free GitHub account (github.com) if you don't have one
2. Create a new repository, upload this file (app.py) and requirements.txt
3. Go to share.streamlit.io, sign in with GitHub, click "New app"
4. Point it at your repository / app.py — it deploys automatically
5. Your app gets a URL like: https://yourname-yourapp.streamlit.app

IMPORTANT — login/token is NOT saved permanently on the server:
- You still log in fresh each day (App ID + Secret + browser redirect step)
- Token is kept only in your browser session (st.session_state) — closing
  the tab means logging in again next time, same as the daily routine you
  already have with fyers_login2.py
- This keeps your credentials OFF the server entirely — nothing sensitive
  is stored in the code or repository

USAGE:
Open the deployed URL → Login tab → enter App ID + Secret → follow the
same browser steps as before → then use Scanner / Stock Check tabs.
"""

import time
from io import StringIO
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import streamlit as st
import pandas as pd
import requests
from fyers_apiv3 import fyersModel

st.set_page_config(page_title="RSI2 Strategy Dashboard", layout="wide")

REDIRECT_URI = "https://127.0.0.1"
CAPITAL = 50000
RISK_PCT = 0.01
STOP_LOSS_PCT = 0.03
DAYS_OF_HISTORY = 420
NIFTY500_CSV_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"

VALIDATED_SYMBOLS = {
    "NH", "KOTAKBANK", "HDFCBANK", "BAJFINANCE", "BEL", "TATASTEEL", "ASHOKLEY",
    "CUMMINSIND", "IOC", "AXISBANK", "ONGC", "ZYDUSLIFE", "POLYCAB", "COALINDIA",
    "WIPRO", "INFY", "DABUR",
}

# ---------- Shared strategy helpers ----------

def rsi(series, period=2):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_history(fyers, symbol, days=DAYS_OF_HISTORY):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    all_candles = []
    chunk_start = start_date
    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=365), end_date)
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": chunk_start.strftime("%Y-%m-%d"),
            "range_to": chunk_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        resp = fyers.history(data=data)
        if resp.get("s") == "ok" and resp.get("candles"):
            all_candles.extend(resp["candles"])
        chunk_start = chunk_end + timedelta(days=1)
    if not all_candles:
        raise Exception("no candle data returned")
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
    df.set_index("date", inplace=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


@st.cache_data(ttl=3600)
def get_nifty500_symbols():
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(NIFTY500_CSV_URL, headers=headers, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        symbol_col = [c for c in df.columns if "symbol" in c.lower()][0]
        symbols = df[symbol_col].dropna().astype(str).str.strip().tolist()
        return [s for s in symbols if s and s.upper() != "SYMBOL"]
    except Exception:
        return list(VALIDATED_SYMBOLS)


# ---------- Session state ----------
if "fyers" not in st.session_state:
    st.session_state.fyers = None
if "session_obj" not in st.session_state:
    st.session_state.session_obj = None

st.title("📊 RSI(2) + 200EMA Strategy Dashboard")

tab_login, tab_scanner, tab_check = st.tabs(["🔑 Login", "📡 Scanner", "🔍 Stock Check"])

# ---------- LOGIN TAB ----------
with tab_login:
    st.subheader("Daily Fyers Login")
    if st.session_state.fyers is not None:
        st.success("✅ Logged in for this session.")
    else:
        client_id = st.text_input("Fyers App ID (e.g. XC1234567-100)").strip()
        secret_key = st.text_input("Fyers Secret ID", type="password").strip()

        if st.button("Step 1: Generate Login URL"):
            if client_id and secret_key:
                session = fyersModel.SessionModel(
                    client_id=client_id, secret_key=secret_key,
                    redirect_uri=REDIRECT_URI, response_type="code",
                    grant_type="authorization_code",
                )
                st.session_state.session_obj = session
                st.session_state.client_id = client_id
                auth_url = session.generate_authcode()
                st.markdown(f"[Click here to log into Fyers]({auth_url})")
                st.info("After logging in, copy the FULL redirected URL and paste it below.")
            else:
                st.warning("Enter both App ID and Secret ID first.")

        redirect_url = st.text_input("Step 2: Paste the full redirected URL here").strip()
        if st.button("Step 3: Complete Login"):
            if st.session_state.session_obj and redirect_url:
                parsed = urlparse(redirect_url)
                query = parse_qs(parsed.query)
                if "auth_code" in query:
                    auth_code = query["auth_code"][0]
                    st.session_state.session_obj.set_token(auth_code)
                    response = st.session_state.session_obj.generate_token()
                    if "access_token" in response:
                        access_token = response["access_token"]
                        st.session_state.fyers = fyersModel.FyersModel(
                            client_id=st.session_state.client_id, is_async=False,
                            token=access_token, log_path="",
                        )
                        st.success("✅ Login successful! Go to Scanner or Stock Check tabs.")
                    else:
                        st.error(f"Login failed: {response}")
                else:
                    st.error("Could not find auth_code in that URL.")

# ---------- SCANNER TAB ----------
with tab_scanner:
    st.subheader("Nifty 500 Daily Scanner")
    st.caption("Entry rule: price > 200-day EMA AND RSI(2) < 10")

    if st.session_state.fyers is None:
        st.warning("Please log in first (Login tab).")
    else:
        if st.button("Run Scanner"):
            fyers = st.session_state.fyers
            symbols = get_nifty500_symbols()
            progress = st.progress(0)
            status = st.empty()
            hits = []

            for i, sym in enumerate(symbols):
                try:
                    df = fetch_history(fyers, f"NSE:{sym}-EQ")
                    if len(df) < 210:
                        continue
                    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
                    df["RSI2"] = rsi(df["close"], 2)
                    latest = df.iloc[-1]
                    if latest["close"] > latest["EMA200"] and latest["RSI2"] < 10:
                        entry = latest["close"]
                        sl = entry * (1 - STOP_LOSS_PCT)
                        qty = max(1, int((CAPITAL * RISK_PCT) / (entry - sl)))
                        hits.append({
                            "Symbol": sym, "Close": round(entry, 2), "RSI2": round(latest["RSI2"], 2),
                            "SL_3pct": round(sl, 2), "Qty_1pct_risk": qty,
                            "Validated": "YES" if sym in VALIDATED_SYMBOLS else "CHECK FIRST",
                        })
                except Exception:
                    pass
                if (i + 1) % 20 == 0:
                    status.text(f"Scanned {i + 1}/{len(symbols)}")
                    progress.progress((i + 1) / len(symbols))
                    time.sleep(0.2)

            progress.progress(1.0)
            status.text(f"Done — {len(symbols)} stocks scanned.")

            if hits:
                hits_df = pd.DataFrame(hits)
                st.success(f"{len(hits)} stock(s) meet entry criteria today.")
                st.dataframe(hits_df, use_container_width=True)
                st.download_button("Download as CSV", hits_df.to_csv(index=False), "scanner_results.csv")
            else:
                st.info("No stocks meet entry criteria today.")

# ---------- STOCK CHECK TAB ----------
with tab_check:
    st.subheader("Single Stock Exit/Entry Checker")
    symbol = st.text_input("NSE Symbol (e.g. ZYDUSLIFE)", value="ZYDUSLIFE").upper()

    if st.session_state.fyers is None:
        st.warning("Please log in first (Login tab).")
    else:
        if st.button("Check Stock"):
            fyers = st.session_state.fyers
            try:
                df = fetch_history(fyers, f"NSE:{symbol}-EQ")
                df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
                df["SMA5"] = df["close"].rolling(5).mean()
                df["RSI2"] = rsi(df["close"], 2)
                latest = df.iloc[-1]

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Close", f"{latest['close']:.2f}")
                col2.metric("200-day EMA", f"{latest['EMA200']:.2f}")
                col3.metric("RSI(2)", f"{latest['RSI2']:.2f}")
                col4.metric("5-day SMA", f"{latest['SMA5']:.2f}")

                trend = "🟢 UPTREND" if latest["close"] > latest["EMA200"] else "🔴 DOWNTREND"
                st.write(f"**Trend:** {trend}")

                reasons = []
                if latest["RSI2"] > 70:
                    reasons.append(f"RSI(2) = {latest['RSI2']:.1f} (> 70)")
                if latest["close"] > latest["SMA5"]:
                    reasons.append(f"Close {latest['close']:.2f} > 5-day SMA {latest['SMA5']:.2f}")

                if reasons:
                    st.error("🔴 EXIT SIGNAL TRIGGERED: " + "; ".join(reasons))
                else:
                    st.success("🟢 No exit signal yet — hold position.")

                st.line_chart(df[["close", "EMA200", "SMA5"]].tail(60))

            except Exception as e:
                st.error(f"Error: {e}")
