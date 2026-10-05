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
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📝 觀察清單 (Sell Put)", "💼 現貨庫存 (Covered Call)", "🚀 訊號推播與日誌", "🎯 活躍部位 (Active Options)", "📊 歷史損益 (Trade History)"])

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
                sym = row['symbol'].strip().upper()
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
                
            for row in display_data:
                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns([2,2,2,3,2])
                    c1.metric("標的", row['標的'])
                    c2.metric("最新股價", row['最新股價'])
                    
                    rsi_val = row['14日 RSI']
                    c3.metric("RSI", f"{rsi_val} 🚨" if isinstance(rsi_val, (int, float)) and rsi_val < 30 else rsi_val)
                    
                    iv_val = row['IV Rank (%)']
                    c4.metric("IV Rank", f"{iv_val}% 🔥" if isinstance(iv_val, (int, float)) and iv_val > 50 else (f"{iv_val}%" if iv_val != 'N/A' else 'N/A'), help=f"財報: {row['下期財報日']}")
                    
                    with c5:
                        st.write("")
                        st.write("")
                        if st.button("🗑️ 刪除", key=f"del_wl_{row['ID']}", use_container_width=True):
                            supabase.table("watchlist").delete().eq("id", row['ID']).execute()
                            st.rerun()
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
                sym = row['symbol'].strip().upper()
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
                
            for row in display_data:
                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 2])
                    c1.metric("標的", row['標的'])
                    c2.metric("持股成本", row['持股成本'])
                    c3.metric("未實現損益", row['未實現損益'])
                    
                    rsi_val = row['14日 RSI']
                    c4.metric("RSI", f"{rsi_val} 🚨" if isinstance(rsi_val, (int, float)) and rsi_val > 70 else rsi_val)
                    
                    with c5:
                        st.write("")
                        st.write("")
                        if st.button("🗑️ 刪除", key=f"del_pf_{row['ID']}", use_container_width=True):
                            supabase.table("portfolio").delete().eq("id", row['ID']).execute()
                            st.rerun()
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
            sp_max_delta = st.number_input("Sell Put 最大 Delta (絕對值)", value=0.30, step=0.01, min_value=0.01, max_value=0.50)
            sp_avoid_earnings = st.checkbox("避開財報日 (推薦合約不跨越財報)", value=True, key="sp_earn")
            
        with col_cc:
            st.markdown("**Covered Call (現貨庫存) 條件**")
            cc_rsi_threshold = st.number_input("RSI 高於此值 (超買)", value=70, step=1, max_value=100, min_value=0)
            cc_iv_threshold = st.number_input("IV (%) 高於此值 (CC)", value=30.0, step=1.0, min_value=0.0)
            cc_max_delta = st.number_input("Covered Call 最大 Delta (絕對值)", value=0.30, step=0.01, min_value=0.01, max_value=0.50)
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
                    options = get_real_put_option(symbol, price, sp_max_delta, sp_avoid_earnings, earnings_date)
                    if not options: continue
                    
                    earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                    
                    opts_msg = ""
                    for opt in options:
                        opts_msg += (f"👉 <b>{opt['dte']}天後到期 ${opt['strike']} Put</b>\n"
                                     f"    Delta: {opt['delta']} | 權利金: ${opt['premium']} | <b>年化: {opt['annualized_return']}%</b> (OI: {int(opt.get('oi', 0))})\n")
                    
                    msg = (
                        f"🚨 <b>Sell Put 訊號觸發！</b>\n"
                        f"標的：${symbol} (目前股價 ${price}){earn_str}\n"
                        f"指標：RSI = {rsi} | IV(%) = {iv}\n"
                        f"-------------------------\n"
                        f"推薦合約：\n{opts_msg}"
                        f"💡 請至 Firstrade 手動掛出限價單"
                    )
                    send_telegram_message(msg)
                    log_signal_to_db(symbol, "SP", f"Price: {price}, RSI: {rsi}, IV: {iv}, Options: {len(options)}")
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
                    options = get_real_call_option(symbol, price, cost_basis, cc_max_delta, cc_avoid_earnings, earnings_date)
                    if not options: continue
                    
                    earn_str = f"\n⚠️ <b>下次財報日：{earnings_date}</b>" if earnings_date else ""
                    
                    opts_msg = ""
                    for opt in options:
                        opts_msg += (f"👉 <b>{opt['dte']}天後到期 ${opt['strike']} Call</b>\n"
                                     f"    Delta: {opt['delta']} | 權利金: ${opt['premium']} | <b>年化: {opt['annualized_return']}%</b> (OI: {int(opt.get('oi', 0))})\n")
                                     
                    msg = (
                        f"🎯 <b>Covered Call 訊號觸發！</b>\n"
                        f"標的：${symbol} (目前股價 ${price}) | 持股成本: ${cost_basis}{earn_str}\n"
                        f"指標：RSI = {rsi} | IV(%) = {iv}\n"
                        f"-------------------------\n"
                        f"推薦合約：\n{opts_msg}"
                        f"💡 履約價已高於成本，請至 Firstrade 賣出開倉"
                    )
                    send_telegram_message(msg)
                    log_signal_to_db(symbol, "CC", f"Price: {price}, Cost: {cost_basis}, RSI: {rsi}, IV: {iv}, Options: {len(options)}")
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


# ----------------- Tab 4: Active Options -----------------
with tab4:
    st.subheader("🎯 活躍選擇權部位管理")
    
    with st.form("add_active_option_form", clear_on_submit=True):
        st.markdown("### 新增部位")
        col1, col2, col3 = st.columns(3)
        ao_symbol = col1.text_input("標的代碼 (例: TSLA)").upper().strip()
        ao_type = col2.selectbox("合約類型", ["Put", "Call"])
        ao_strike = col3.number_input("履約價", min_value=0.0, step=1.0)
        
        col4, col5, col6 = st.columns(3)
        import datetime
        ao_exp = col4.date_input("到期日", value=datetime.date.today() + datetime.timedelta(days=30))
        ao_premium = col5.number_input("收取權利金 (單口)", min_value=0.0, step=0.01)
        ao_qty = col6.number_input("口數", min_value=1, step=1)
        
        submitted_ao = st.form_submit_button("新增部位")
        if submitted_ao and ao_symbol and ao_strike > 0 and ao_premium > 0:
            try:
                supabase.table("active_options").insert({
                    "symbol": ao_symbol,
                    "option_type": ao_type,
                    "strike": float(ao_strike),
                    "expiration_date": str(ao_exp),
                    "premium_received": float(ao_premium),
                    "quantity": int(ao_qty)
                }).execute()
                st.success(f"已新增 {ao_symbol} {ao_type} 部位")
                st.rerun()
            except Exception as e:
                st.error(f"寫入資料庫失敗: {e}")

    st.divider()
    
    try:
        active_data = supabase.table("active_options").select("*").order("created_at", desc=True).execute().data
        if active_data:
            st.markdown("### 📊 目前持有部位狀態")
            display_ao = []
            
            with st.spinner("抓取最新合約報價中..."):
                for row in active_data:
                    sym = row['symbol'].strip().upper()
                    strike = float(row['strike'])
                    exp_date_str = row['expiration_date']
                    opt_type = row['option_type'].lower()
                    premium_rec = float(row['premium_received'])
                    
                    exp_date = datetime.datetime.strptime(exp_date_str, "%Y-%m-%d").date()
                    dte = (exp_date - datetime.date.today()).days
                    
                    # 抓取即時市價
                    current_price = 0.0
                    current_ask = 0.0
                    debug_error = ""
                    try:
                        ticker = yf.Ticker(sym)
                        available_exps = ticker.options
                        if available_exps:
                            closest_exp = min(available_exps, key=lambda x: abs(datetime.datetime.strptime(x, "%Y-%m-%d").date() - exp_date))
                            opt = ticker.option_chain(closest_exp)
                            chain = opt.puts if opt_type == 'put' else opt.calls
                            match = chain[abs(chain['strike'] - strike) < 0.05]
                            if not match.empty:
                                bid = match.iloc[0]['bid']
                                ask = match.iloc[0]['ask']
                                last = match.iloc[0]['lastPrice']
                                current_ask = ask if not pd.isna(ask) and ask > 0 else last
                                current_price = (bid + ask) / 2 if (not pd.isna(bid) and not pd.isna(ask) and bid > 0 and ask > 0) else last
                            else:
                                debug_error = f"找不到履約價 {strike}"
                        else:
                            debug_error = "找不到任何到期日"
                    except Exception as e:
                        debug_error = str(e)
                    
                    # 狀態判定 (保守估計用 Ask 計算停利，避免滑價)
                    status_list = []
                    profit_pct = 0.0
                    
                    if current_ask > 0:
                        profit_pct = ((premium_rec - current_ask) / premium_rec) * 100
                        if profit_pct >= 50:
                            status_list.append("🎯 建議停利 (>50%)")
                        elif profit_pct < -50:
                            status_list.append("📉 嚴重浮虧")
                    
                    if 0 < dte <= 21:
                        status_list.append("⚠️ 建議轉倉 (DTE≤21)")
                    elif dte <= 0:
                        status_list.append("🚨 已到期")
                        
                    status_str = " | ".join(status_list) if status_list else "🟢 持有中"
                        
                    display_ao.append({
                        "ID": row['id'],
                        "symbol": sym,
                        "type": row['option_type'],
                        "strike": strike,
                        "exp": exp_date_str,
                        "dte": dte,
                        "status": status_str,
                        "premium": premium_rec,
                        "current_price": current_price,
                        "current_ask": current_ask,
                        "profit_pct": profit_pct,
                        "qty": int(row['quantity']),
                        "debug_error": debug_error
                    })
            
            # 使用卡片來美化呈現
            for ao in display_ao:
                with st.container(border=True):
                    c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 3, 2])
                    c1.markdown(f"**{ao['symbol']}** ${ao['strike']} {ao['type']}")
                    c1.caption(f"到期日: {ao['exp']} ({ao['dte']}天)")
                    
                    c2.metric("收取權利金", f"${ao['premium']:.2f}")
                    
                    # 以 Mid Price 顯示現價，但 tooltip 標示 Ask
                    if ao['current_price'] > 0:
                        c3.metric("當前中價 (Mid)", f"${ao['current_price']:.2f}", help=f"賣價 (Ask): ${ao['current_ask']:.2f}")
                    else:
                        c3.metric("當前中價 (Mid)", "N/A", help=ao.get('debug_error', '抓取失敗'))
                    
                    # 損益百分比
                    pct_str = f"{ao['profit_pct']:.1f}%" if ao['current_ask'] > 0 else "N/A"
                    c4.metric("目前狀態與建議", ao['status'], delta=pct_str if pct_str != "N/A" else None)
                    
                    with c5:
                        st.write("")
                        st.write("")
                        # 如果是建議停利，按鈕變更明顯
                        btn_type = "primary" if "建議停利" in ao['status'] else "secondary"
                        # 這裡的平倉將導引他去下方的結算表單 (因此按鈕只做為視覺提醒，或直接帶入下方的表單)
                        st.markdown(f"*口數: {ao['qty']}*")
            
            st.divider()
            st.markdown("### 結算平倉 (Close Position)")
            close_options = {f"{r['symbol']} {r['expiration_date']} ${r['strike']} {r['option_type']}": r for r in active_data}
            
            with st.form("close_position_form"):
                selected_pos_str = st.selectbox("選擇要平倉的部位", list(close_options.keys()))
                close_price = st.number_input("平倉買回權利金 (單口成本，若到期失效歸零請輸入 0)", min_value=0.0, step=0.01, value=0.0)
                
                is_roll = st.checkbox("🔄 這是轉倉 (Roll Out) 操作？ (結算當前部位，同時建立新部位)")
                st.markdown("---")
                colA, colB, colC = st.columns(3)
                new_exp = colA.date_input("新合約到期日 (轉倉才需填寫)", value=datetime.date.today() + datetime.timedelta(days=30))
                new_strike = colB.number_input("新合約履約價 (轉倉才需填寫)", min_value=0.0, step=1.0)
                new_premium = colC.number_input("新收取權利金 (轉倉才需填寫)", min_value=0.0, step=0.01)
                
                submitted_close = st.form_submit_button("確認送出 (平倉/轉倉)")
                
                if submitted_close and selected_pos_str:
                    pos = close_options[selected_pos_str]
                    qty = int(pos['quantity'])
                    premium_rec = float(pos['premium_received'])
                    # 每口 100 股
                    pnl = (premium_rec - close_price) * 100 * qty
                    
                    try:
                        # 1. 紀錄平倉到 trade_history
                        supabase.table("trade_history").insert({
                            "symbol": pos['symbol'],
                            "option_type": pos['option_type'],
                            "strike": pos['strike'],
                            "expiration_date": pos['expiration_date'],
                            "open_date": pos['open_date'],
                            "premium_received": premium_rec,
                            "premium_paid": close_price,
                            "quantity": qty,
                            "pnl": pnl
                        }).execute()
                        
                        # 2. 如果是轉倉，建立新部位
                        if is_roll and new_strike > 0 and new_premium > 0:
                            supabase.table("active_options").insert({
                                "symbol": pos['symbol'],
                                "option_type": pos['option_type'],
                                "strike": float(new_strike),
                                "expiration_date": str(new_exp),
                                "premium_received": float(new_premium),
                                "quantity": qty
                            }).execute()
                        
                        # 3. 刪除舊部位
                        supabase.table("active_options").delete().eq("id", pos['id']).execute()
                        
                        if is_roll:
                            st.success(f"轉倉成功！舊合約平倉損益: ${pnl:.2f}。已為您建立新部位。")
                        else:
                            st.success(f"平倉成功！該筆交易總損益為: ${pnl:.2f}")
                            
                        st.rerun()
                    except Exception as e:
                        st.error(f"執行失敗，請確保 trade_history 等資料表存在: {e}")
        else:
            st.info("目前沒有任何活躍部位。")
    except Exception as e:
        st.error(f"讀取活躍部位失敗: {e}")

# ----------------- Tab 5: Trade History -----------------
with tab5:
    st.subheader("📊 歷史損益與交易紀錄 (Trade History)")
    try:
        history_data = supabase.table("trade_history").select("*").order("close_date", desc=True).execute().data
        if history_data:
            display_hist = []
            total_pnl = 0.0
            
            for row in history_data:
                pnl = float(row['pnl'])
                total_pnl += pnl
                display_hist.append({
                    "平倉日": row['close_date'],
                    "標的": row['symbol'],
                    "類型": row['option_type'],
                    "履約價": float(row['strike']),
                    "到期日": row['expiration_date'],
                    "建倉權利金": float(row['premium_received']),
                    "平倉權利金": float(row['premium_paid']),
                    "口數": int(row['quantity']),
                    "單筆損益": pnl
                })
                
            st.metric("累積已實現損益 (Total Realized P&L)", f"${total_pnl:.2f}")
            
            df_hist = pd.DataFrame(display_hist)
            # 將單筆損益依照正負加上顏色
            def color_pnl(val):
                color = 'green' if val > 0 else 'red' if val < 0 else 'white'
                return f'color: {color}'
            st.dataframe(df_hist.style.applymap(color_pnl, subset=['單筆損益']), use_container_width=True, hide_index=True)
            
        else:
            st.info("目前尚無平倉歷史紀錄。")
    except Exception as e:
        st.error(f"讀取歷史紀錄失敗 (請確認 trade_history 資料表是否已建立): {e}")


