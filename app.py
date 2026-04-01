from flask import Flask, render_template, jsonify, request
import yfinance as yf
import sqlite3
import threading
import time
import requests
import pandas as pd
import os
import math
from datetime import datetime

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "db.sqlite")
PORT = int(os.getenv("PORT", 10000))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SE_WATCHLIST = [
    "VOLV-B.ST", "ERIC-B.ST", "ABB.ST", "SEB-A.ST", "SHB-A.ST",
    "ATCO-B.ST", "SAND.ST", "SKF-B.ST", "ASSA-B.ST", "NDA-SE.ST"
]

US_WATCHLIST = [
    "NVDA", "MSFT", "AAPL", "AMD", "TSLA", "META", "AMZN", "PLTR"
]


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT UNIQUE NOT NULL,
            buy_price REAL NOT NULL,
            trailing_percent REAL NOT NULL DEFAULT 0.05,
            highest_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            last_price REAL,
            alert_sent INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def now_str():
    return datetime.utcnow().isoformat()


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        return float(v)
    except Exception:
        return default


def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ALERT]", message)
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception as e:
        print("Telegramfel:", e)


def fetch_history(ticker: str, period="6mo", interval="1d") -> pd.DataFrame:
    df = yf.download(
        tickers=ticker,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False
    )

    if df is None or df.empty:
        raise ValueError(f"Ingen marknadsdata för {ticker}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]

    df = df.rename(columns=str.title)

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col not in df.columns:
            raise ValueError(f"Saknar kolumn {col} för {ticker}")

    df = df.dropna(subset=["Close"]).copy()
    return df


def calculate_indicators(ticker: str) -> pd.DataFrame:
    df = fetch_history(ticker, period="6mo", interval="1d")

    df["EMA10"] = df["Close"].ewm(span=10, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

    delta = df["Close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)

    roll_up = up.ewm(alpha=1/14, adjust=False).mean()
    roll_down = down.ewm(alpha=1/14, adjust=False).mean()

    rs = roll_up / roll_down.replace(0, pd.NA)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["RSI"] = df["RSI"].fillna(50)

    df["VOL20"] = df["Volume"].rolling(20).mean()

    return df


def get_latest_price(ticker: str) -> float:
    df = fetch_history(ticker, period="5d", interval="1d")
    return float(df["Close"].dropna().iloc[-1])


def score_stock(ticker: str):
    try:
        df = calculate_indicators(ticker)
        if len(df) < 25:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-20]

        change_20d = (last["Close"] - prev["Close"]) / prev["Close"]
        ema_ok = last["EMA10"] > last["EMA20"]
        rsi_ok = 45 <= last["RSI"] <= 72
        volume_ok = safe_float(last["Volume"], 0) > 200_000 or safe_float(last["VOL20"], 0) > 200_000

        score = 0
        if change_20d > 0.05:
            score += 2
        elif change_20d > 0.02:
            score += 1

        if ema_ok:
            score += 2
        if rsi_ok:
            score += 1
        if volume_ok:
            score += 1

        if score < 4:
            return None

        return {
            "ticker": ticker,
            "score": score,
            "price": round(float(last["Close"]), 2),
            "change_20d": round(change_20d * 100, 2),
            "rsi": round(float(last["RSI"]), 1)
        }
    except Exception as e:
        print(f"score_stock error {ticker}: {e}")
        return None


def get_hot_stocks():
    candidates = []
    for ticker in SE_WATCHLIST + US_WATCHLIST:
        result = score_stock(ticker)
        if result:
            candidates.append(result)

    candidates.sort(key=lambda x: (x["score"], x["change_20d"]), reverse=True)
    return candidates[:8]


def add_stock(ticker: str, trailing_percent: float = 0.05, note: str = ""):
    ticker = ticker.strip().upper()
    price = get_latest_price(ticker)
    now = now_str()

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT id FROM stocks WHERE ticker = ?", (ticker,))
    row = c.fetchone()

    if row:
        conn.close()
        return {
            "ticker": ticker,
            "price": price,
            "status": "exists"
        }

    c.execute("""
        INSERT INTO stocks (
            ticker, buy_price, trailing_percent, highest_price, stop_price,
            last_price, alert_sent, note, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker,
        price,
        trailing_percent,
        price,
        price * (1 - trailing_percent),
        price,
        0,
        note,
        now,
        now
    ))

    conn.commit()
    conn.close()

    return {
        "ticker": ticker,
        "price": price,
        "status": "added"
    }


def refresh_portfolio_prices():
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute("SELECT * FROM stocks").fetchall()

    for row in rows:
        try:
            price = get_latest_price(row["ticker"])
            c.execute("""
                UPDATE stocks
                SET last_price = ?, updated_at = ?
                WHERE id = ?
            """, (price, now_str(), row["id"]))
        except Exception as e:
            print(f"refresh error for {row['ticker']}: {e}")

    conn.commit()
    conn.close()


def get_status(last_price: float, stop_price: float):
    if last_price <= stop_price:
        return "TRÄFFAD"

    distance = (last_price - stop_price) / stop_price
    if distance <= 0.02:
        return "VARNING"

    return "OK"


def monitor_loop():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            rows = c.execute("SELECT * FROM stocks").fetchall()

            for row in rows:
                try:
                    ticker = row["ticker"]
                    trailing_percent = float(row["trailing_percent"])
                    highest_price = float(row["highest_price"])
                    stop_price = float(row["stop_price"])
                    alert_sent = int(row["alert_sent"])

                    current_price = get_latest_price(ticker)

                    if current_price > highest_price:
                        highest_price = current_price
                        stop_price = highest_price * (1 - trailing_percent)
                        alert_sent = 0

                    triggered = current_price <= stop_price

                    if triggered and not alert_sent:
                        send_telegram_alert(
                            f"STOP-LOSS TRÄFFAD\n"
                            f"{ticker}\n"
                            f"Pris: {current_price:.2f}\n"
                            f"Stop: {stop_price:.2f}"
                        )
                        alert_sent = 1

                    c.execute("""
                        UPDATE stocks
                        SET highest_price = ?, stop_price = ?, last_price = ?,
                            alert_sent = ?, updated_at = ?
                        WHERE id = ?
                    """, (
                        current_price if current_price > highest_price else highest_price,
                        stop_price,
                        current_price,
                        alert_sent,
                        now_str(),
                        row["id"]
                    ))

                except Exception as inner_e:
                    print(f"monitor error {row['ticker']}: {inner_e}")

            conn.commit()
            conn.close()

        except Exception as e:
            print("monitor loop error:", e)

        time.sleep(300)  # var 5:e minut


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/portfolio")
def portfolio():
    refresh_portfolio_prices()

    conn = get_conn()
    rows = conn.execute("""
        SELECT *
        FROM stocks
        ORDER BY updated_at DESC, ticker ASC
    """).fetchall()
    conn.close()

    data = []
    for row in rows:
        last_price = safe_float(row["last_price"], 0)
        stop_price = safe_float(row["stop_price"], 0)

        data.append({
            "id": row["id"],
            "ticker": row["ticker"],
            "buy_price": round(float(row["buy_price"]), 2),
            "highest_price": round(float(row["highest_price"]), 2),
            "stop_price": round(float(row["stop_price"]), 2),
            "last_price": round(float(row["last_price"]), 2) if row["last_price"] is not None else None,
            "trailing_percent": round(float(row["trailing_percent"]) * 100, 2),
            "note": row["note"] or "",
            "alert_sent": bool(row["alert_sent"]),
            "status": get_status(last_price, stop_price)
        })

    return jsonify(data)


@app.route("/add", methods=["POST"])
def add():
    payload = request.get_json(force=True)
    ticker = payload.get("ticker", "").strip().upper()
    trailing_percent = safe_float(payload.get("trailing_percent"), 0.05)
    note = payload.get("note", "").strip()

    if not ticker:
        return jsonify({"error": "Ticker saknas"}), 400

    if trailing_percent <= 0 or trailing_percent >= 0.5:
        return jsonify({"error": "trailing_percent måste vara mellan 0 och 0.5"}), 400

    result = add_stock(ticker, trailing_percent, note)
    return jsonify(result)


@app.route("/delete/<int:stock_id>", methods=["DELETE"])
def delete_stock(stock_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM stocks WHERE id = ?", (stock_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/trending")
def trending():
    hot = get_hot_stocks()
    added = []

    for stock in hot:
        try:
            result = add_stock(stock["ticker"], 0.05, "Tillagd från heta aktier")
            added.append({
                "ticker": stock["ticker"],
                "price": stock["price"],
                "change_20d": stock["change_20d"],
                "score": stock["score"],
                "status": result["status"]
            })
        except Exception as e:
            print("trending add error:", e)

    return jsonify({
        "count": len(added),
        "items": added
    })


@app.route("/candlestick/<ticker>")
def candlestick(ticker):
    ticker = ticker.upper()
    df = calculate_indicators(ticker).reset_index()

    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])

    stop_line = df["Close"] * 0.95

    return jsonify({
        "ticker": ticker,
        "date": df[date_col].dt.strftime("%Y-%m-%d").tolist(),
        "open": [safe_float(x, None) for x in df["Open"].tolist()],
        "high": [safe_float(x, None) for x in df["High"].tolist()],
        "low": [safe_float(x, None) for x in df["Low"].tolist()],
        "close": [safe_float(x, None) for x in df["Close"].tolist()],
        "ema10": [safe_float(x, None) for x in df["EMA10"].tolist()],
        "ema20": [safe_float(x, None) for x in df["EMA20"].tolist()],
        "rsi": [safe_float(x, None) for x in df["RSI"].tolist()],
        "stop_line": [safe_float(x, None) for x in stop_line.tolist()]
    })


if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
else:
    init_db()
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()