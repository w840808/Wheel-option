import os
import time
import datetime
import requests
import pandas as pd
import yfinance as yf
import math
from bs4 import BeautifulSoup
from supabase import create_client, Client
from dotenv import load_dotenv

# 載入環境變數 (可以在本地建立一個 .env 檔案來存放)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([SUPABASE_URL, SUPABASE_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ 缺少環境變數，請檢查 .env 檔案設定。")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# 核心函式 (與 app.py 相同)
# ==========================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_bs_delta(S, K, t, r, sigma, option_type="call"):
    if t <= 0 or sigma <= 0: return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    if option_type == "call": return norm_cdf(d1)
    else: return norm_cdf(d1) - 1.0

def calculate_rsi(symbol: str, window: int = 14):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="3mo")
        if df.empty or len(df) < window: return None, None
        close = df['Close']
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.ewm(alpha=1/window, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/window, adjust=False).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(float(close.iloc[-1]), 2), round(float(rsi.iloc[-1]), 2)
    except:
        return None, None

def get_iv_rank(symbol: str) -> float:
    try:
        url = f"https://www.alphaquery.com/stock/{symbol.upper()}/volatility-option-statistics/30-day/iv-mean"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            iv_element = soup.find('div', string='Implied Volatility (Mean)')
            if iv_element:
                try:
                    iv_value_str = iv_element.find_parent('tr').find('div', class_='indicator-figure-inner').text.strip()
                    return round(float(iv_value_str) * 100, 2)
                except:
                    pass
        return 0.0
    except:
        return 0.0

def get_real_put_option(symbol: str, current_price: float, target_delta: float = 0.15):
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        target_dates = [d for d in valid_dates if 30 <= d[1] <= 45]
        if not target_dates:
            target_date = today + datetime.timedelta(days=30)
            target_dates = [min(valid_dates, key=lambda x: abs(x[1] - 30))]
        best_date, dte = target_dates[0]
        opt = ticker.option_chain(best_date)
        puts = opt.puts
        otm_puts = puts[puts['strike'] < current_price].copy()
        if otm_puts.empty: return None
        t_years = dte / 365.0
        deltas = []
        for idx, row in otm_puts.iterrows():
            iv = row['impliedVolatility']
            if iv == 0 or pd.isna(iv): iv = 0.01
            deltas.append(abs(calculate_bs_delta(current_price, row['strike'], t_years, 0.04, iv, "put")))
        otm_puts['delta_abs'] = deltas
        otm_puts['delta_dist'] = (otm_puts['delta_abs'] - target_delta).abs()
        best_put = otm_puts.sort_values('delta_dist').iloc[0]
        premium = (best_put['bid'] + best_put['ask']) / 2 if best_put['bid'] > 0 else best_put['lastPrice']
        return {"dte": dte, "strike": float(best_put['strike']), "delta": round(-best_put['delta_abs'], 3), "premium": round(float(premium), 2), "type": "Put"}
    except:
        return None

def get_real_call_option(symbol: str, current_price: float, cost_basis: float, target_delta: float = 0.15):
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        target_dates = [d for d in valid_dates if 30 <= d[1] <= 45]
        if not target_dates:
            target_date = today + datetime.timedelta(days=30)
            target_dates = [min(valid_dates, key=lambda x: abs(x[1] - 30))]
        best_date, dte = target_dates[0]
        opt = ticker.option_chain(best_date)
        calls = opt.calls
        min_strike = max(current_price, cost_basis)
        otm_calls = calls[calls['strike'] >= min_strike].copy()
        if otm_calls.empty: return None
        t_years = dte / 365.0
        deltas = []
        for idx, row in otm_calls.iterrows():
            iv = row['impliedVolatility']
            if iv == 0 or pd.isna(iv): iv = 0.01
            deltas.append(calculate_bs_delta(current_price, row['strike'], t_years, 0.04, iv, "call"))
        otm_calls['delta'] = deltas
        otm_calls['delta_dist'] = (otm_calls['delta'] - target_delta).abs()
        best_call = otm_calls.sort_values('delta_dist').iloc[0]
        premium = (best_call['bid'] + best_call['ask']) / 2 if best_call['bid'] > 0 else best_call['lastPrice']
        return {"dte": dte, "strike": float(best_call['strike']), "delta": round(best_call['delta'], 3), "premium": round(float(premium), 2), "type": "Call"}
    except:
        return None

def send_telegram_message(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram 推播失敗: {e}")

def log_signal_to_db(symbol: str, signal_type: str, details: str):
    try:
        supabase.table("signal_logs").insert({"symbol": symbol, "signal_type": signal_type, "details": details}).execute()
    except Exception as e:
        print(f"日誌寫入失敗: {e}")

# ==========================================
# 掃描任務主程式
# ==========================================
def run_scan():
    print(f"[{datetime.datetime.now()}] 開始執行自動掃描任務...")
    
    # 預設參數 (可根據需要修改或設計從 DB 讀取)
    SP_RSI_THRESH, SP_IV_THRESH, SP_DELTA = 35, 50.0, 0.15
    CC_RSI_THRESH, CC_IV_THRESH, CC_DELTA = 70, 30.0, 0.15

    # 1. 掃描 Sell Put
    watchlist = supabase.table("watchlist").select("*").execute().data
    if watchlist:
        for row in watchlist:
            symbol = row["symbol"]
            price, rsi = calculate_rsi(symbol)
            if not price or not rsi: continue
            iv = get_iv_rank(symbol)
            
            if rsi < SP_RSI_THRESH and iv > SP_IV_THRESH:
                option = get_real_put_option(symbol, price, SP_DELTA)
                if not option: continue
                msg = (
                    f"🚨 <b>【自動掃描】Sell Put 訊號觸發！</b>\n"
                    f"標的：${symbol} (現價 ${price})\n"
                    f"指標：RSI = {rsi} | IV = {iv}%\n"
                    f"-------------------------\n"
                    f"推薦合約：{option['dte']}天後到期 ${option['strike']} Put\n"
                    f"Delta: {option['delta']} | 預估權利金: ${option['premium']}"
                )
                send_telegram_message(msg)
                log_signal_to_db(symbol, "SP_AUTO", f"Price: {price}, RSI: {rsi}, IV: {iv}, Strike: {option['strike']}, DTE: {option['dte']}")
                print(f"✅ 已推播 {symbol} Sell Put 訊號")

    # 2. 掃描 Covered Call
    portfolio = supabase.table("portfolio").select("*").execute().data
    if portfolio:
        for row in portfolio:
            symbol = row["symbol"]
            cost_basis = row["cost_basis"]
            price, rsi = calculate_rsi(symbol)
            if not price or not rsi: continue
            iv = get_iv_rank(symbol)
            
            if rsi > CC_RSI_THRESH and iv > CC_IV_THRESH:
                option = get_real_call_option(symbol, price, cost_basis, CC_DELTA)
                if not option: continue
                msg = (
                    f"🎯 <b>【自動掃描】Covered Call 訊號觸發！</b>\n"
                    f"標的：${symbol} (現價 ${price}) | 成本: ${cost_basis}\n"
                    f"指標：RSI = {rsi} | IV = {iv}%\n"
                    f"-------------------------\n"
                    f"推薦合約：{option['dte']}天後到期 ${option['strike']} Call\n"
                    f"Delta: {option['delta']} | 預估權利金: ${option['premium']}"
                )
                send_telegram_message(msg)
                log_signal_to_db(symbol, "CC_AUTO", f"Price: {price}, RSI: {rsi}, IV: {iv}, Strike: {option['strike']}, DTE: {option['dte']}")
                print(f"✅ 已推播 {symbol} Covered Call 訊號")
                
    print(f"[{datetime.datetime.now()}] 掃描任務完成！")

if __name__ == "__main__":
    run_scan()
