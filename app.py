import streamlit as st
from supabase import create_client, Client
import yfinance as yf
import pandas as pd
import requests
import random
import datetime
from bs4 import BeautifulSoup
import math

def norm_cdf(x):
    """標準常態分配的累計機率密度函數近似值"""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_bs_delta(S, K, t, r, sigma, option_type="call"):
    """計算 Black-Scholes Delta"""
    if t <= 0 or sigma <= 0: return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    if option_type == "call": return norm_cdf(d1)
    else: return norm_cdf(d1) - 1.0

# ==========================================
# 1. 初始化與環境變數設定
# ==========================================
st.set_page_config(page_title="美股輪盤策略監控面板", layout="wide")
st.title("🎡 美股輪盤策略 (The Wheel) 監控與推播面板")

# 初始化 Supabase
@st.cache_resource
def init_supabase() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        st.error(f"Supabase 初始化失敗，請檢查 st.secrets 設定: {e}")
        st.stop()

supabase = init_supabase()

# ==========================================
# 2. 核心邏輯與 Mock 函式
# ==========================================

def calculate_rsi(symbol: str, window: int = 14):
    """取得最新股價與 14 日 RSI"""
    try:
        ticker = yf.Ticker(symbol)
        # 抓取最近 3 個月的資料以確保有足夠長度計算 RMA (Wilder's Smoothing)
        df = ticker.history(period="3mo")
        if df.empty or len(df) < window:
            return None, None
        
        close = df['Close']
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.ewm(alpha=1/window, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/window, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(float(close.iloc[-1]), 2), round(float(rsi.iloc[-1]), 2)
    except Exception as e:
        st.warning(f"無法取得 {symbol} 的股價或 RSI: {e}")
        return None, None

def get_iv_rank(symbol: str) -> float:
    """從 AlphaQuery 爬取指定美股的 IV (30-Day)，若在雲端被擋則改算 30天歷史波動率 (HV)"""
    try:
        url = f"https://www.alphaquery.com/stock/{symbol.upper()}/volatility-option-statistics/30-day/iv-mean"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            iv_element = soup.find('div', string='Implied Volatility (Mean)')
            if iv_element:
                try:
                    iv_value_str = iv_element.find_parent('tr').find('div', class_='indicator-figure-inner').text.strip()
                    iv_value = float(iv_value_str) * 100  # 轉為百分比
                    return round(iv_value, 2)
                except:
                    pass
    except:
        pass
        
    # 備用方案：使用 yfinance 計算 1年期歷史波動率位階 (Historical Volatility Rank, HV Rank)
    try:
        import numpy as np
        ticker = yf.Ticker(symbol)
        # 取過去一年的資料來計算
        df = ticker.history(period="1y")
        if not df.empty and len(df) > 50:
            df['Return'] = df['Close'].pct_change()
            # 滾動計算過去 30 天的年化歷史波動率
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
    """從 yfinance 取得下一次財報日"""
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

def get_real_put_option(symbol: str, current_price: float, target_delta: float = 0.15, avoid_earnings: bool = False, earnings_date: datetime.date = None):
    """從 yfinance 取得真實 Sell Put 候選合約 (尋找最接近目標 Delta)"""
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
            
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        
        if avoid_earnings and earnings_date:
            valid_dates = [d for d in valid_dates if datetime.datetime.strptime(d[0], "%Y-%m-%d").date() < earnings_date]
        if not valid_dates: return None
        
        target_dates = [d for d in valid_dates if 14 <= d[1] <= 45]
        
        if not target_dates:
            best_match = min(valid_dates, key=lambda x: abs(x[1] - 30))
            target_dates = [best_match]
            
        best_date, dte = target_dates[0]
        opt = ticker.option_chain(best_date)
        puts = opt.puts
        
        otm_puts = puts[puts['strike'] < current_price].copy()
        if otm_puts.empty: return None
        
        t_years = dte / 365.0
        r = 0.04 # 假設 4% 無風險利率
        
        deltas = []
        for idx, row in otm_puts.iterrows():
            iv = row['impliedVolatility']
            if iv == 0 or pd.isna(iv): iv = 0.01
            delta = calculate_bs_delta(current_price, row['strike'], t_years, r, iv, "put")
            deltas.append(abs(delta))
            
        otm_puts['delta_abs'] = deltas
        otm_puts['delta_dist'] = (otm_puts['delta_abs'] - target_delta).abs()
        best_put = otm_puts.sort_values('delta_dist').iloc[0]
        
        premium = (best_put['bid'] + best_put['ask']) / 2 if best_put['bid'] > 0 else best_put['lastPrice']
            
        return {
            "dte": dte, 
            "strike": float(best_put['strike']), 
            "delta": round(-best_put['delta_abs'], 3),
            "premium": round(float(premium), 2), 
            "type": "Put"
        }
    except Exception as e:
        return None

def get_real_call_option(symbol: str, current_price: float, cost_basis: float, target_delta: float = 0.15, avoid_earnings: bool = False, earnings_date: datetime.date = None):
    """從 yfinance 取得真實 Covered Call 候選合約 (尋找最接近目標 Delta)"""
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations: return None
            
        today = datetime.date.today()
        valid_dates = [(d, (datetime.datetime.strptime(d, "%Y-%m-%d").date() - today).days) for d in expirations]
        
        if avoid_earnings and earnings_date:
            valid_dates = [d for d in valid_dates if datetime.datetime.strptime(d[0], "%Y-%m-%d").date() < earnings_date]
        if not valid_dates: return None
        
        target_dates = [d for d in valid_dates if 14 <= d[1] <= 45]
        
        if not target_dates:
            best_match = min(valid_dates, key=lambda x: abs(x[1] - 30))
            target_dates = [best_match]
            
        best_date, dte = target_dates[0]
        opt = ticker.option_chain(best_date)
        calls = opt.calls
        
        min_strike = max(current_price, cost_basis)
        otm_calls = calls[calls['strike'] >= min_strike].copy()
        if otm_calls.empty: return None
        
        t_years = dte / 365.0
        r = 0.04
        
        deltas = []
        for idx, row in otm_calls.iterrows():
            iv = row['impliedVolatility']
            if iv == 0 or pd.isna(iv): iv = 0.01
            delta = calculate_bs_delta(current_price, row['strike'], t_years, r, iv, "call")
            deltas.append(delta)
            
        otm_calls['delta'] = deltas
        otm_calls['delta_dist'] = (otm_calls['delta'] - target_delta).abs()
        best_call = otm_calls.sort_values('delta_dist').iloc[0]
        
        premium = (best_call['bid'] + best_call['ask']) / 2 if best_call['bid'] > 0 else best_call['lastPrice']
            
        return {
            "dte": dte, 
            "strike": float(best_call['strike']), 
            "delta": round(best_call['delta'], 3),
            "premium": round(float(premium), 2), 
            "type": "Call"
        }
    except Exception as e:
        return None

def send_telegram_message(message: str):
    """發送 Telegram 推播通知"""
    try:
        token = st.secrets["TELEGRAM_BOT_TOKEN"]
        chat_id = st.secrets["CHAT_ID"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload)
        if res.status_code != 200:
            st.error(f"推播失敗: {res.text}")
    except Exception as e:
        st.error(f"Telegram 設定錯誤或連線失敗: {e}")

def log_signal_to_db(symbol: str, signal_type: str, details: str):
    """將觸發的訊號寫入 Supabase 日誌表格"""
    try:
        supabase.table("signal_logs").insert({
            "symbol": symbol,
            "signal_type": signal_type,
            "details": details
        }).execute()
    except Exception as e:
        st.error(f"寫入日誌失敗: {e}")

# ==========================================
# 3. 頁面與 UI 介面設計
# ==========================================
tab1, tab2, tab3 = st.tabs(["📋 觀察清單 (Sell Put)", "💼 現貨庫存 (Covered Call)", "🚀 訊號掃描與日誌"])

# ----------------- Tab 1: Watchlist -----------------
with tab1:
    st.subheader("Sell Put 觀察清單")
    
    # 新增標的
    with st.form("add_watchlist_form", clear_on_submit=True):
        col1, col2 = st.columns([3, 1])
        new_symbol = col1.text_input("新增標的代號 (例如 PLTR)").upper().strip()
        submitted = col2.form_submit_button("新增標的")
        if submitted and new_symbol:
            # 檢查是否已存在
            existing = supabase.table("watchlist").select("*").eq("symbol", new_symbol).execute()
            if not existing.data:
                supabase.table("watchlist").insert({"symbol": new_symbol}).execute()
                st.success(f"已新增 {new_symbol}")
                st.rerun()
            else:
                st.warning(f"{new_symbol} 已在清單中")

    # 顯示清單與即時數據
    watchlist_data = supabase.table("watchlist").select("*").order("created_at", desc=True).execute().data
    if watchlist_data:
        st.markdown("### 📊 即時監控數據")
        with st.spinner("載入最新數據中..."):
            display_data = []
            for row in watchlist_data:
                sym = row['symbol']
                price, rsi = calculate_rsi(sym)
                ivr = get_iv_rank(sym)
                earn_date = get_next_earnings(sym)
                display_data.append({
                    "ID": row['id'],
                    "標的": sym,
                    "最新股價": f"${price}" if price else "N/A",
                    "14日 RSI": rsi if rsi else "N/A",
                    "IV Rank (%)": ivr,
                    "下期財報日": str(earn_date) if earn_date else "N/A"
                })
                
            df_wl = pd.DataFrame(display_data)
            st.dataframe(df_wl.drop(columns=["ID"]), use_container_width=True, hide_index=True)
        
        st.divider()
        st.markdown("### 🔧 管理清單")
        for row in display_data:
            col1, col2 = st.columns([4, 1])
            col1.markdown(f"**{row['標的']}**")
            if col2.button("刪除", key=f"del_wl_{row['ID']}"):
                supabase.table("watchlist").delete().eq("id", row['ID']).execute()
                st.rerun()
            st.divider()
    else:
        st.info("目前觀察清單為空。")

# ----------------- Tab 2: Portfolio -----------------
with tab2:
    st.subheader("Covered Call 庫存清單")
    
    # 新增標的與成本價
    with st.form("add_portfolio_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 1])
        new_symbol = col1.text_input("新增標的代號 (例如 SOFI)").upper().strip()
        cost_basis = col2.number_input("持股成本價 (Cost Basis)", min_value=0.0, step=0.1)
        submitted = col3.form_submit_button("新增庫存")
        if submitted and new_symbol and cost_basis > 0:
            existing = supabase.table("portfolio").select("*").eq("symbol", new_symbol).execute()
            if not existing.data:
                supabase.table("portfolio").insert({"symbol": new_symbol, "cost_basis": cost_basis}).execute()
                st.success(f"已新增 {new_symbol} (成本: ${cost_basis})")
                st.rerun()
            else:
                # 若已存在則更新成本價
                supabase.table("portfolio").update({"cost_basis": cost_basis}).eq("symbol", new_symbol).execute()
                st.success(f"已更新 {new_symbol} 成本價為 ${cost_basis}")
                st.rerun()

    # 顯示清單與即時數據
    portfolio_data = supabase.table("portfolio").select("*").order("created_at", desc=True).execute().data
    if portfolio_data:
        st.markdown("### 📊 庫存即時數據")
        with st.spinner("載入最新數據中..."):
            display_data = []
            for row in portfolio_data:
                sym = row['symbol']
                cost = row['cost_basis']
                price, rsi = calculate_rsi(sym)
                ivr = get_iv_rank(sym)
                earn_date = get_next_earnings(sym)
                
                pnl_str = "N/A"
                if price:
                    pnl = round(price - cost, 2)
                    pnl_pct = round((pnl / cost) * 100, 2)
                    pnl_str = f"${pnl} ({pnl_pct}%)"
                    
                display_data.append({
                    "ID": row['id'],
                    "標的": sym,
                    "持股成本": f"${cost}",
                    "最新股價": f"${price}" if price else "N/A",
                    "未實現損益": pnl_str,
                    "14日 RSI": rsi if rsi else "N/A",
                    "IV Rank (%)": ivr,
                    "下期財報日": str(earn_date) if earn_date else "N/A"
                })
                
            df_pf = pd.DataFrame(display_data)
            st.dataframe(df_pf.drop(columns=["ID"]), use_container_width=True, hide_index=True)
        
        st.divider()
        st.markdown("### 🔧 管理庫存")
        for row in display_data:
            col1, col2, col3 = st.columns([2, 2, 1])
            col1.markdown(f"**{row['標的']}**")
            col2.markdown(f"成本價: **{row['持股成本']}**")
            if col3.button("刪除", key=f"del_pf_{row['ID']}"):
                supabase.table("portfolio").delete().eq("id", row['ID']).execute()
                st.rerun()
            st.divider()
    else:
        st.info("目前庫存清單為空。")

# ----------------- Tab 3: Scanner & Logs -----------------
with tab3:
    st.subheader("執行全面掃描")
    
    # 增加監控與篩選指標的數值設定
    with st.expander("⚙️ 掃描參數設定", expanded=True):
        col_sp, col_cc = st.columns(2)
        with col_sp:
            st.markdown("**Sell Put (觀察清單) 條件**")
            sp_rsi_threshold = st.number_input("RSI 低於此值 (超賣)", value=45, step=1, max_value=100, min_value=0)
            sp_iv_threshold = st.number_input("IV (%) 高於此值", value=30.0, step=1.0, min_value=0.0)
            sp_target_delta = st.number_input("Sell Put 目標 Delta (絕對值)", value=0.15, step=0.01, min_value=0.01, max_value=0.50)
            sp_avoid_earnings = st.checkbox("避開財報日 (推薦合約不跨越財報)", value=True, key="sp_earn")
            
        with col_cc:
            st.markdown("**Covered Call (現貨庫存) 條件**")
            cc_rsi_threshold = st.number_input("RSI 高於此值 (超買)", value=70, step=1, max_value=100, min_value=0)
            cc_iv_threshold = st.number_input("IV (%) 高於此值 (CC)", value=30.0, step=1.0, min_value=0.0)
            cc_target_delta = st.number_input("Covered Call 目標 Delta", value=0.15, step=0.01, min_value=0.01, max_value=0.50)
            cc_avoid_earnings = st.checkbox("避開財報日 (推薦合約不跨越財報)", value=False, key="cc_earn")

    if st.button("🔍 開始掃描訊號", use_container_width=True, type="primary"):
        with st.spinner("掃描中，請稍候..."):
            
            # --- 邏輯 A: 針對 Watchlist (Sell Put) ---
            watchlist = supabase.table("watchlist").select("*").execute().data
            for row in watchlist:
                symbol = row["symbol"]
                price, rsi = calculate_rsi(symbol)
                if not price or not rsi: continue
                
                iv = get_iv_rank(symbol)
                
                # 根據使用者設定的條件進行判斷
                if rsi < sp_rsi_threshold and iv > sp_iv_threshold:
                    earnings_date = get_next_earnings(symbol)
                    option = get_real_put_option(symbol, price, sp_target_delta, sp_avoid_earnings, earnings_date)
                    if not option: continue
                    
                    earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                    msg = (
                        f"🚨 <b>Sell Put 訊號觸發！</b>\n"
                        f"標的：${symbol} (目前股價 ${price}){earn_str}\n"
                        f"指標：RSI = {rsi} | IV(%) = {iv}\n"
                        f"-------------------------\n"
                        f"推薦合約：{symbol} {option['dte']}天後到期 ${option['strike']} Put\n"
                        f"Delta: {option['delta']} | 預估權利金: ${option['premium']}\n"
                        f"💡 請至 Firstrade 手動掛出限價單"
                    )
                    send_telegram_message(msg)
                    log_signal_to_db(symbol, "SP", f"Price: {price}, RSI: {rsi}, IV: {iv}, Strike: {option['strike']}, DTE: {option['dte']}")
                    st.success(f"[Sell Put] 觸發 {symbol}！")

            # --- 邏輯 B: 針對 Portfolio (Covered Call) ---
            portfolio = supabase.table("portfolio").select("*").execute().data
            for row in portfolio:
                symbol = row["symbol"]
                cost_basis = row["cost_basis"]
                price, rsi = calculate_rsi(symbol)
                if not price or not rsi: continue
                
                iv = get_iv_rank(symbol)
                
                # 根據使用者設定的條件進行判斷
                if rsi > cc_rsi_threshold and iv > cc_iv_threshold:
                    earnings_date = get_next_earnings(symbol)
                    option = get_real_call_option(symbol, price, cost_basis, cc_target_delta, cc_avoid_earnings, earnings_date)
                    if not option: continue
                    
                    earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                    msg = (
                        f"🎯 <b>Covered Call 訊號觸發！</b>\n"
                        f"標的：${symbol} (目前股價 ${price}) | 持股成本: ${cost_basis}{earn_str}\n"
                        f"指標：RSI = {rsi} | IV(%) = {iv}\n"
                        f"-------------------------\n"
                        f"推薦合約：{symbol} {option['dte']}天後到期 ${option['strike']} Call\n"
                        f"Delta: {option['delta']} | 預估權利金: ${option['premium']}\n"
                        f"💡 履約價已高於成本，請至 Firstrade 賣出開倉"
                    )
                    send_telegram_message(msg)
                    log_signal_to_db(symbol, "CC", f"Price: {price}, Cost: {cost_basis}, RSI: {rsi}, IV: {iv}, Strike: {option['strike']}, DTE: {option['dte']}")
                    st.success(f"[Covered Call] 觸發 {symbol}！")
                    
        st.info("掃描完成。")
        
    st.divider()
    st.subheader("📝 最近觸發日誌")
    logs_data = supabase.table("signal_logs").select("*").order("created_at", desc=True).limit(20).execute().data
    
    if logs_data:
        df_logs = pd.DataFrame(logs_data)
        # 整理顯示欄位，如果某些欄位可能缺失，要加強保護
        cols_to_show = ['created_at', 'symbol', 'signal_type', 'details']
        df_logs = df_logs[[c for c in cols_to_show if c in df_logs.columns]]
        
        # 重新命名與格式化
        rename_dict = {
            'created_at': '時間',
            'symbol': '標的',
            'signal_type': '策略',
            'details': '詳細資訊'
        }
        df_logs.rename(columns={k:v for k,v in rename_dict.items() if k in df_logs.columns}, inplace=True)
        
        if '時間' in df_logs.columns:
            # 轉換為台灣時區並格式化
            df_logs['時間'] = pd.to_datetime(df_logs['時間']).dt.tz_convert('Asia/Taipei').dt.strftime('%Y-%m-%d %H:%M:%S')
            
        st.dataframe(df_logs, use_container_width=True, hide_index=True)
    else:
        st.write("目前尚無觸發紀錄。")
