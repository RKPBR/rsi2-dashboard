"""
daily_auto_job.py — Fully automated daily pipeline (runs on GitHub Actions)

Steps each run:
1. Refresh the Fyers access token using refresh_token + PIN (no browser needed)
2. Scan the Nifty 500 for entry signals (price>200EMA AND RSI(2)<10)
3. Run a full in-sample/out-of-sample backtest on each hit
4. Filter to the strict "TRADEABLE TODAY" list (5+ trades, 60%+ win rate,
   0.7+ win/loss ratio, in BOTH periods)
5. Send the result to Telegram

All secrets (App ID, Secret ID, refresh_token, PIN, Telegram bot token,
Telegram chat ID) are read from environment variables — set these as
GitHub Secrets, never hardcode them here.

Required GitHub Secrets:
  FYERS_APP_ID, FYERS_SECRET_ID, FYERS_REFRESH_TOKEN, FYERS_PIN,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import hashlib
import time
from io import StringIO
from datetime import datetime, timedelta

import requests
import pandas as pd
from fyers_apiv3 import fyersModel

CAPITAL = 50000
RISK_PCT = 0.01
STOP_LOSS_PCT = 0.03
DAYS_OF_HISTORY = 420
BACKTEST_DAYS_OF_HISTORY = 900
OUT_OF_SAMPLE_DAYS = 180
NIFTY500_CSV_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"

VALIDATED_SYMBOLS = {
    "NH", "KOTAKBANK", "HDFCBANK", "BAJFINANCE", "BEL", "TATASTEEL", "ASHOKLEY",
    "CUMMINSIND", "IOC", "AXISBANK", "ONGC", "ZYDUSLIFE", "POLYCAB", "COALINDIA",
    "WIPRO", "INFY", "DABUR",
}

APP_ID = os.environ["FYERS_APP_ID"]
SECRET_ID = os.environ["FYERS_SECRET_ID"]
REFRESH_TOKEN = os.environ["FYERS_REFRESH_TOKEN"]
PIN = os.environ["FYERS_PIN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for i in range(0, len(text), 4000):  # split long messages
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text[i:i + 4000]})


def refresh_access_token():
    app_id_hash = hashlib.sha256(f"{APP_ID}:{SECRET_ID}".encode()).hexdigest()
    url = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
    data = {
        "grant_type": "refresh_token",
        "appIdHash": app_id_hash,
        "refresh_token": REFRESH_TOKEN,
        "pin": PIN,
    }
    resp = requests.post(url, json=data, headers={"Content-Type": "application/json"}).json()
    if "access_token" not in resp:
        raise Exception(f"Token refresh failed: {resp}")
    return resp["access_token"]


def rsi(series, period=2):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_history(fyers, symbol, days):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    all_candles = []
    chunk_start = start_date
    while chunk_start < end_date:
        chunk_end = min(chunk_start + timedelta(days=365), end_date)
        data = {
            "symbol": symbol, "resolution": "D", "date_format": "1",
            "range_from": chunk_start.strftime("%Y-%m-%d"),
            "range_to": chunk_end.strftime("%Y-%m-%d"), "cont_flag": "1",
        }
        resp = fyers.history(data=data)
        if resp.get("s") == "ok" and resp.get("candles"):
            all_candles.extend(resp["candles"])
        chunk_start = chunk_end + timedelta(days=1)
    if not all_candles:
        raise Exception("no candle data")
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)
    df.set_index("date", inplace=True)
    return df[~df.index.duplicated(keep="first")].sort_index()


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


def run_scanner(fyers):
    symbols = get_nifty500_symbols()
    hits = []
    for sym in symbols:
        try:
            df = fetch_history(fyers, f"NSE:{sym}-EQ", DAYS_OF_HISTORY)
            if len(df) < 210:
                continue
            df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
            df["RSI2"] = rsi(df["close"], 2)
            latest = df.iloc[-1]
            if latest["close"] > latest["EMA200"] and latest["RSI2"] < 10:
                hits.append(sym)
        except Exception:
            pass
        time.sleep(0.2)
    return hits


def backtest_symbol(symbol, df):
    df = df.copy()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["RSI2"] = rsi(df["close"], 2)
    df["SMA5"] = df["close"].rolling(5).mean()
    cutoff_date = df.index[-1] - timedelta(days=OUT_OF_SAMPLE_DAYS)
    trades = []
    position = None
    for i in range(200, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        if position is None:
            if row["close"] > row["EMA200"] and row["RSI2"] < 10:
                entry_price = row["close"]
                sl_price = entry_price * (1 - STOP_LOSS_PCT)
                qty = max(1, int((CAPITAL * RISK_PCT) / (entry_price - sl_price)))
                position = {"entry_date": date, "entry_price": entry_price, "sl_price": sl_price, "qty": qty}
        else:
            exit_price, exit_reason = None, None
            if row["low"] <= position["sl_price"]:
                exit_price, exit_reason = position["sl_price"], "SL"
            elif row["RSI2"] > 70:
                exit_price, exit_reason = row["close"], "RSI2>70"
            elif row["close"] > row["SMA5"]:
                exit_price, exit_reason = row["close"], "Close>SMA5"
            if exit_price is not None:
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                trades.append({"Symbol": symbol, "PnL": pnl, "is_out_of_sample": date >= cutoff_date})
                position = None
    return trades


def summarize(trades_df):
    if trades_df.empty:
        return pd.DataFrame()
    grouped = trades_df.groupby("Symbol").agg(
        Trades=("PnL", "count"),
        WinRate=("PnL", lambda x: round((x > 0).mean() * 100, 1)),
        AvgWin=("PnL", lambda x: x[x > 0].mean() if (x > 0).any() else 0),
        AvgLoss=("PnL", lambda x: x[x <= 0].mean() if (x <= 0).any() else 0),
    ).reset_index()
    grouped["RR_Ratio"] = grouped.apply(
        lambda r: round(r["AvgWin"] / abs(r["AvgLoss"]), 2) if r["AvgLoss"] != 0 else 0, axis=1
    )
    return grouped


def main():
    try:
        access_token = refresh_access_token()
    except Exception as e:
        send_telegram(f"⚠️ RSI2 bot: token refresh FAILED — your refresh_token has likely expired "
                       f"(15-day limit). Run get_refresh_token.py again and update GitHub Secrets.\n\nError: {e}")
        return

    fyers = fyersModel.FyersModel(client_id=APP_ID, is_async=False, token=access_token, log_path="")

    today_str = datetime.now().strftime("%Y-%m-%d")

    try:
        hits = run_scanner(fyers)
    except Exception as e:
        send_telegram(f"⚠️ RSI2 bot: scanner failed on {today_str}\nError: {e}")
        return

    if not hits:
        send_telegram(f"📊 RSI2 Strategy — {today_str}\n\nNo stocks met entry criteria today.")
        return

    all_trades = []
    for sym in hits:
        try:
            df = fetch_history(fyers, f"NSE:{sym}-EQ", BACKTEST_DAYS_OF_HISTORY)
            if len(df) < 210:
                continue
            all_trades.extend(backtest_symbol(sym, df))
        except Exception:
            pass

    if not all_trades:
        send_telegram(f"📊 RSI2 Strategy — {today_str}\n\n{len(hits)} scanner hit(s), "
                       f"but not enough history to backtest them.")
        return

    trades_df = pd.DataFrame(all_trades)
    in_sample = summarize(trades_df[~trades_df["is_out_of_sample"]])
    out_sample = summarize(trades_df[trades_df["is_out_of_sample"]])

    message_lines = [f"📊 RSI2 Strategy — {today_str}", f"Scanner hits: {len(hits)}\n"]

    if not in_sample.empty and not out_sample.empty:
        merged = in_sample.merge(out_sample, on="Symbol", suffixes=("_IN", "_OUT"))
        qualified = merged[
            (merged["Trades_IN"] >= 5) & (merged["Trades_OUT"] >= 5) &
            (merged["WinRate_IN"] >= 60) & (merged["WinRate_OUT"] >= 60) &
            (merged["RR_Ratio_IN"] >= 0.7) & (merged["RR_Ratio_OUT"] >= 0.7)
        ]
        if not qualified.empty:
            message_lines.append("✅ TRADEABLE TODAY:\n")
            for _, r in qualified.iterrows():
                validated = "✓validated" if r["Symbol"] in VALIDATED_SYMBOLS else "check first"
                message_lines.append(
                    f"{r['Symbol']} ({validated})\n"
                    f"  In: {r['Trades_IN']}tr/{r['WinRate_IN']}%/{r['RR_Ratio_IN']}RR  "
                    f"Out: {r['Trades_OUT']}tr/{r['WinRate_OUT']}%/{r['RR_Ratio_OUT']}RR"
                )
        else:
            message_lines.append("No stock passed the strict TRADEABLE filter today.")
    else:
        message_lines.append("Not enough trade history to evaluate filters today.")

    send_telegram("\n".join(message_lines))


if __name__ == "__main__":
    main()
