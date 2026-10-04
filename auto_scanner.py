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
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            iv_element = soup.find('div', string='Implied Volatility (Mean)')
            if iv_element:
                try:
                    iv_value_str = iv_element.find_parent('tr').find('div', class_='indicator-figure-inner').text.strip()
                    return round(float(iv_value_str) * 100, 2)
                except:
                    pass
    except:
        pass
        
    try:
        import numpy as np
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1y")
        if not df.empty and len(df) > 50:
            df['Return'] = df['Close'].pct_change()
            hv = df['Return'].rolling(window=30).std() * (252 ** 0.5) * 100
            hv = hv.dropna()
            if not hv.empty:
                current_hv = hv.iloc[-1]
                min_hv = hv.min()
                max_hv = hv.max()
                if max_hv > min_hv:
                    hv_rank = (current_hv - min_hv) / (max_hv - min_hv) * 100
                    return round(hv_rank, 2)
    except:
        pass
        
    return 0.0

def get_next_earnings(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        calendar = ticker.calendar
        if calendar and 'Earnings Date' in calendar and calendar['Earnings Date']:
            dates = calendar['Earnings Date']
            today = datetime.date.today()
            upcoming = [d for d in dates if d >= today]
            if upcoming:
                return upcoming[0]
    except:
        pass
    return None

def get_real_put_option(symbol: str, current_price: float, max_delta: float = 0.3, avoid_earnings: bool = False, earnings_date: datetime.date = None):
    try:
        import datetime
        import pandas as pd
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
        
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        
        if avoid_earnings and earnings_date:
            valid_dates = [d for d in valid_dates if datetime.datetime.strptime(d[0], "%Y-%m-%d").date() < earnings_date]
        if not valid_dates: return None
        
        target_dates = [d for d in valid_dates if 10 <= d[1] <= 45]
        
        if not target_dates:
            best_match = min(valid_dates, key=lambda x: abs(x[1] - 30))
            target_dates = [best_match]
            
        all_options = []
        r = 0.04
        
        for date_str, dte in target_dates:
            try:
                opt = ticker.option_chain(date_str)
                puts = opt.puts
                otm_puts = puts[puts['strike'] < current_price].copy()
                if otm_puts.empty: continue
                
                t_years = dte / 365.0
                
                for idx, row in otm_puts.iterrows():
                    iv = row['impliedVolatility']
                    if iv == 0 or pd.isna(iv): iv = 0.40
                    
                    bid = row['bid']
                    ask = row['ask']
                    oi = row['openInterest']
                    
                    # 流動性與價差過濾 (Liquidity & Spread Filters)
                    if pd.isna(bid) or bid <= 0: continue
                    if pd.isna(oi) or oi < 10: continue
                    # 若價差大於 $0.20 且價差比例超過 30%，則視為流動性過差跳過
                    if (ask - bid) > 0.20 and (ask - bid) / bid > 0.30: continue
                    
                    delta = calculate_bs_delta(current_price, row['strike'], t_years, r, iv, "put")
                    delta_abs = abs(delta)
                    
                    if delta_abs < max_delta and delta_abs > 0.05:
                        premium = (bid + ask) / 2
                        strike = float(row['strike'])
                        ar = (premium / strike) * (365 / dte) * 100 if strike > 0 and dte > 0 else 0
                        
                        all_options.append({
                            "dte": dte, 
                            "strike": strike, 
                            "delta": round(-delta_abs, 3),
                            "premium": round(float(premium), 2), 
                            "annualized_return": round(ar, 2),
                            "type": "Put",
                            "oi": oi
                        })
            except:
                continue
                
        if not all_options: return None
        
        all_options.sort(key=lambda x: x['annualized_return'], reverse=True)
        
        results = []
        seen_strikes = set()
        for opt in all_options:
            if opt['strike'] not in seen_strikes:
                seen_strikes.add(opt['strike'])
                results.append(opt)
                if len(results) >= 2:
                    break
        return results
    except Exception as e:
        return None

def get_real_call_option(symbol: str, current_price: float, cost_basis: float, max_delta: float = 0.3, avoid_earnings: bool = False, earnings_date: datetime.date = None):
    try:
        import datetime
        import pandas as pd
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
            
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        
        if avoid_earnings and earnings_date:
            valid_dates = [d for d in valid_dates if datetime.datetime.strptime(d[0], "%Y-%m-%d").date() < earnings_date]
        if not valid_dates: return None
        
        target_dates = [d for d in valid_dates if 10 <= d[1] <= 45]
        
        if not target_dates:
            best_match = min(valid_dates, key=lambda x: abs(x[1] - 30))
            target_dates = [best_match]
            
        all_options = []
        r = 0.04
        
        for date_str, dte in target_dates:
            try:
                opt = ticker.option_chain(date_str)
                calls = opt.calls
                
                min_strike = max(current_price, cost_basis)
                otm_calls = calls[calls['strike'] >= min_strike].copy()
                if otm_calls.empty: continue
                
                t_years = dte / 365.0
                
                for idx, row in otm_calls.iterrows():
                    iv = row['impliedVolatility']
                    if iv == 0 or pd.isna(iv): iv = 0.40
                    
                    bid = row['bid']
                    ask = row['ask']
                    oi = row['openInterest']
                    
                    # 流動性與價差過濾 (Liquidity & Spread Filters)
                    if pd.isna(bid) or bid <= 0: continue
                    if pd.isna(oi) or oi < 10: continue
                    if (ask - bid) > 0.20 and (ask - bid) / bid > 0.30: continue
                    
                    delta = calculate_bs_delta(current_price, row['strike'], t_years, r, iv, "call")
                    
                    if delta < max_delta and delta > 0.05:
                        premium = (bid + ask) / 2
                        strike = float(row['strike'])
                        ar = (premium / current_price) * (365 / dte) * 100 if current_price > 0 and dte > 0 else 0
                        
                        all_options.append({
                            "dte": dte, 
                            "strike": strike, 
                            "delta": round(delta, 3),
                            "premium": round(float(premium), 2), 
                            "annualized_return": round(ar, 2),
                            "type": "Call",
                            "oi": oi
                        })
            except:
                continue
                
        if not all_options: return None
        
        all_options.sort(key=lambda x: x['annualized_return'], reverse=True)
        
        results = []
        seen_strikes = set()
        for opt in all_options:
            if opt['strike'] not in seen_strikes:
                seen_strikes.add(opt['strike'])
                results.append(opt)
                if len(results) >= 2:
                    break
        return results
    except Exception as e:
        return None

# ==========================================
# 掃描任務主程式
# ==========================================
def check_active_options():
    print(f"[{datetime.datetime.now()}] 開始檢查活躍選擇權倉位 (Take Profit / Rolling)...")
    try:
        active_options = supabase.table("active_options").select("*").execute().data
        if not active_options:
            return
            
        for position in active_options:
            symbol = position['symbol']
            strike = float(position['strike'])
            exp_date_str = position['expiration_date']
            opt_type = position['option_type'].lower()
            premium_received = float(position['premium_received'])
            
            exp_date = datetime.datetime.strptime(exp_date_str, "%Y-%m-%d").date()
            today = datetime.date.today()
            dte = (exp_date - today).days
            
            # 1. 轉倉提醒 (恰好 21 DTE 時提醒一次)
            if dte == 21:
                msg = f"⚠️ <b>轉倉提醒 (21 DTE)</b>\n"
                msg += f"標的: ${symbol}\n"
                msg += f"合約: {exp_date_str} 到期 ${strike} {opt_type.capitalize()}\n"
                msg += f"建議: 考慮平倉或向後延期 (Roll Out) 以降低 Gamma 風險！\n"
                send_telegram_message(msg)
                
            if dte <= 0:
                continue
                
            # 2. 停利檢查 (50% 最大利潤)
            ticker = yf.Ticker(symbol)
            try:
                opt = ticker.option_chain(exp_date_str)
                chain = opt.puts if opt_type == 'put' else opt.calls
                match = chain[chain['strike'] == strike]
                if not match.empty:
                    current_bid = match.iloc[0]['bid']
                    current_ask = match.iloc[0]['ask']
                    # 賣出選擇權平倉是「買回」，看 Ask (如果沒有 Ask 看 lastPrice)
                    current_price = current_ask if current_ask > 0 else match.iloc[0]['lastPrice']
                    
                    if current_price > 0 and current_price <= premium_received * 0.5:
                        msg = f"🎯 <b>停利通知 (50% 獲利達標)</b>\n"
                        msg += f"標的: ${symbol}\n"
                        msg += f"合約: {exp_date_str} 到期 ${strike} {opt_type.capitalize()}\n"
                        msg += f"當前平倉成本: ${current_price:.2f} (原收取: ${premium_received:.2f})\n"
                        msg += f"建議: 利潤已達 50%，建議買回平倉釋放資金效率！\n"
                        send_telegram_message(msg)
            except Exception as e:
                print(f"無法檢查 {symbol} 的合約: {e}")
                
    except Exception as e:
        print(f"檢查活躍倉位發生錯誤 (可能尚未建立表格): {e}")

def run_scan():
    print(f"[{datetime.datetime.now()}] 開始執行自動掃描任務...")
    
    # 預設參數 (可根據需要修改或設計從 DB 讀取)
    SP_RSI_THRESH, SP_IV_THRESH, SP_DELTA = 45, 30.0, 0.15
    CC_RSI_THRESH, CC_IV_THRESH, CC_DELTA = 70, 30.0, 0.15
    SP_AVOID_EARNINGS = True
    CC_AVOID_EARNINGS = False

    # 1. 掃描 Sell Put
    watchlist = supabase.table("watchlist").select("*").execute().data
    if watchlist:
        for row in watchlist:
            symbol = row["symbol"]
            price, rsi = calculate_rsi(symbol)
            if not price or not rsi: continue
            iv = get_iv_rank(symbol)
            
            if rsi < SP_RSI_THRESH and iv > SP_IV_THRESH:
                earnings_date = get_next_earnings(symbol)
                options = get_real_put_option(symbol, price, 0.30, SP_AVOID_EARNINGS, earnings_date)
                if not options: continue
                earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                
                opts_msg = ""
                for opt in options:
                    opts_msg += (f"👉 <b>{opt['dte']}天後到期 ${opt['strike']} Put</b>\n"
                                 f"    Delta: {opt['delta']} | 權利金: ${opt['premium']} | <b>年化: {opt['annualized_return']}%</b>\n")
                                 
                msg = (
                    f"🚨 <b>【自動掃描】Sell Put 訊號觸發！</b>\n"
                    f"標的：${symbol} (現價 ${price}){earn_str}\n"
                    f"指標：RSI = {rsi} | IV = {iv}%\n"
                    f"-------------------------\n"
                    f"推薦合約：\n{opts_msg}"
                )
                send_telegram_message(msg)
                log_signal_to_db(symbol, "SP_AUTO", f"Price: {price}, RSI: {rsi}, IV: {iv}, Options: {len(options)}")
                print(f"[OK] 已推播 {symbol} Sell Put 訊號")

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
                earnings_date = get_next_earnings(symbol)
                options = get_real_call_option(symbol, price, cost_basis, [0.15, 0.25], CC_AVOID_EARNINGS, earnings_date)
                if not options: continue
                earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                
                opts_msg = ""
                for opt in options:
                    opts_msg += (f"👉 <b>{opt['dte']}天後到期 ${opt['strike']} Call</b>\n"
                                 f"    Delta: {opt['delta']} | 權利金: ${opt['premium']} | <b>年化: {opt['annualized_return']}%</b>\n")
                                 
                msg = (
                    f"🎯 <b>【自動掃描】Covered Call 訊號觸發！</b>\n"
                    f"標的：${symbol} (現價 ${price}) | 成本: ${cost_basis}{earn_str}\n"
                    f"指標：RSI = {rsi} | IV = {iv}%\n"
                    f"-------------------------\n"
                    f"推薦合約：\n{opts_msg}"
                )
                send_telegram_message(msg)
                log_signal_to_db(symbol, "CC_AUTO", f"Price: {price}, RSI: {rsi}, IV: {iv}, Options: {len(options)}")
                print(f"[OK] 已推播 {symbol} Covered Call 訊號")
                
    print(f"[{datetime.datetime.now()}] 掃描任務完成！")

if __name__ == "__main__":
    run_scan()
