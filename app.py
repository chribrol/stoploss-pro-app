from flask import Flask, render_template, jsonify
import yfinance as yf
import sqlite3, threading, time, requests
import pandas as pd

app = Flask(__name__)

# Telegram-inställningar
TELEGRAM_TOKEN = ""  # Sätt din token här
CHAT_ID = ""         # Sätt ditt chat id här

def send_alert(msg):
    if TELEGRAM_TOKEN:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

# Initiera databasen
def init_db():
    conn = sqlite3.connect("db.sqlite")
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS stocks (
        id INTEGER PRIMARY KEY,
        ticker TEXT,
        buy_price REAL,
        trailing_percent REAL,
        highest_price REAL,
        stop_price REAL
    )""")
    conn.commit()
    conn.close()
init_db()

# Beräkna EMA och RSI
def calculate_indicators(ticker):
    df = yf.Ticker(ticker).history(period="30d")
    df['EMA10'] = df['Close'].ewm(span=10, adjust=False).mean()
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    delta = df['Close'].diff()
    up, down = delta.clip(lower=0), -delta.clip(upper=0)
    roll_up = up.rolling(14).mean()
    roll_down = down.rolling(14).mean()
    rs = roll_up / roll_down
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def get_price(ticker):
    data = yf.Ticker(ticker)
    return float(data.history(period="1d")["Close"].iloc[-1])

# Heta aktier SE + US
def get_trending():
    try:
        data = requests.get("https://query1.finance.yahoo.com/v1/finance/trending/US").json()
        symbols = [i["symbol"] for i in data["finance"]["result"][0]["quotes"]][:20]
    except:
        symbols = []
    extra = ["VOLV-B.ST", "ERIC-B.ST", "ABB.ST", "SEB-A.ST", "SHB-A.ST"]
    symbols += extra
    selected = []
    for s in symbols:
        try:
            df = calculate_indicators(s)
            change = (df['Close'].iloc[-1] - df['Close'].iloc[0])/df['Close'].iloc[0]
            volume = yf.Ticker(s).info.get("volume",0)
            ema_signal = df['EMA10'].iloc[-1] > df['EMA20'].iloc[-1]
            rsi_ok = 30 < df['RSI'].iloc[-1] < 70
            if change>0.03 and volume>1_000_000 and ema_signal and rsi_ok:
                selected.append(s)
        except:
            continue
    return list(dict.fromkeys(selected))[:5]

# Lägg trending i DB
def add_trending_to_db():
    symbols = get_trending()
    conn = sqlite3.connect("db.sqlite"); c=conn.cursor()
    for ticker in symbols:
        price = get_price(ticker)
        c.execute("SELECT * FROM stocks WHERE ticker=?", (ticker,))
        if c.fetchone(): continue
        c.execute("""
        INSERT INTO stocks (ticker, buy_price, trailing_percent, highest_price, stop_price)
        VALUES (?, ?, ?, ?, ?)""", (ticker, price, 0.05, price, price*0.95))
    conn.commit(); conn.close()

# Stop loss monitor med trailing
def monitor():
    while True:
        conn = sqlite3.connect("db.sqlite"); c=conn.cursor()
        rows = c.execute("SELECT * FROM stocks").fetchall()
        for row in rows:
            id, ticker, buy, percent, high, stop = row
            price = get_price(ticker)
            if price > high: high, stop = price, price*(1-percent)
            triggered = price <= stop
            if triggered: send_alert(f" SÄLJ {ticker}! Pris: {price:.2f}")
            c.execute("UPDATE stocks SET highest_price=?, stop_price=? WHERE id=?", (high, stop, id))
        conn.commit(); conn.close()
        time.sleep(300)

threading.Thread(target=monitor).start()

# Routes
@app.route("/")
def index(): return render_template("index.html")

@app.route("/trending")
def trending(): add_trending_to_db(); return jsonify({"status":"added"})

@app.route("/portfolio")
def portfolio():
    conn = sqlite3.connect("db.sqlite"); c=conn.cursor()
    rows = c.execute("SELECT * FROM stocks").fetchall()
    conn.close()
    data=[]
    for r in rows:
        id, ticker, buy, percent, high, stop = r
        data.append({"ticker": ticker, "price": get_price(ticker), "stop": stop})
    return jsonify(data)

@app.route("/candlestick/<ticker>")
def candlestick(ticker):
    df = calculate_indicators(ticker)
    df = df.reset_index()
    data = {
        "date": df['Date'].dt.strftime('%Y-%m-%d').tolist(),
        "open": df['Open'].tolist(),
        "high": df['High'].tolist(),
        "low": df['Low'].tolist(),
        "close": df['Close'].tolist(),
        "EMA10": df['EMA10'].tolist(),
        "EMA20": df['EMA20'].tolist(),
        "RSI": df['RSI'].tolist()
    }
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)