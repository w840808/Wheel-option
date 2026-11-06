import os
import sys
import time
import datetime
import threading
import requests
import toml
from supabase import create_client, Client

# 設定 Windows 控制台標準輸出為 UTF-8
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==========================================
# 載入環境變數與金鑰
# ==========================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

secrets_path = os.path.join(os.path.dirname(__file__), ".streamlit", "secrets.toml")
if os.path.exists(secrets_path):
    try:
        sec = toml.load(secrets_path)
        SUPABASE_URL = SUPABASE_URL or sec.get("SUPABASE_URL")
        SUPABASE_KEY = SUPABASE_KEY or sec.get("SUPABASE_KEY")
        TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN or sec.get("TELEGRAM_BOT_TOKEN")
        CHAT_ID = CHAT_ID or sec.get("CHAT_ID")
    except Exception as e:
        print(f"載入 secrets.toml 失敗: {e}")

if not all([SUPABASE_URL, SUPABASE_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ 缺少必要連線金鑰，請檢查 secrets.toml 或環境變數。")
    sys.exit(1)

CHAT_ID = str(CHAT_ID).strip()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 匯入 auto_scanner 核心分析工具
from auto_scanner import (
    calculate_rsi,
    get_iv_rank,
    get_next_earnings,
    get_real_put_option,
    get_real_call_option,
    fetch_stock_price,
    fetch_option_prices,
    run_scan,
    check_active_options
)

# 快捷按鈕配置
DEFAULT_KEYBOARD = {
    "keyboard": [
        [{"text": "🔍 立即掃描訊號"}, {"text": "🎯 查詢活躍部位"}],
        [{"text": "📊 歷史損益統計"}, {"text": "📋 觀察清單行情"}],
        [{"text": "ℹ️ 使用說明 / 指令"}]
    ],
    "resize_keyboard": True
}

# ==========================================
# Telegram 輔助工具函式
# ==========================================
def send_reply(chat_id, text, reply_markup=DEFAULT_KEYBOARD):
    """即時回覆訊息至指定 chat_id（不受夜間守衛攔截）"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup
    }
    try:
        res = requests.post(url, json=payload, timeout=12)
        return res.status_code == 200
    except Exception as e:
        print(f"發送訊息異常: {e}")
        return False

def register_bot_commands():
    """向 Telegram 註冊斜線指令選單"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "scan", "description": "立即掃描全市場策略訊號"},
        {"command": "pos", "description": "查詢當前所有未平倉選擇權部位"},
        {"command": "hist", "description": "查詢累積損益統計與平倉紀錄"},
        {"command": "watch", "description": "查看觀察清單股價、RSI 與 IV"},
        {"command": "help", "description": "顯示完整操作說明與指南"}
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
    except Exception:
        pass

# ==========================================
# 業務邏輯處理器
# ==========================================
def handle_scan(chat_id):
    """執行即時掃描並回傳結果"""
    send_reply(chat_id, "⏳ 正在啟動即時掃描（檢查部位 + 掃描清單），請稍候 15~30 秒...")
    try:
        found_count = run_scan(force=True)
        if found_count == 0:
            msg = (
                "✅ <b>掃描任務完成！</b>\n\n"
                "目前觀察清單中的標的尚未觸發超賣或高 IV 條件 (RSI < 45, IV > 30%)，"
                "無新的推薦合約。市場暫無極端訊號，請耐心等待最佳開倉時機。"
            )
            send_reply(chat_id, msg)
        else:
            send_reply(chat_id, f"✅ <b>掃描完成！共發現 {found_count} 個符合策略門檻的推薦合約。</b>")
    except Exception as e:
        send_reply(chat_id, f"❌ 執行掃描時發生錯誤: {e}")

def handle_positions(chat_id):
    """查詢活躍選擇權部位"""
    send_reply(chat_id, "⏳ 正在連接市場抓取當前持倉最新行情與 Greeks...")
    try:
        positions = supabase.table("active_options").select("*").order("expiration_date", desc=False).execute().data
        if not positions:
            send_reply(chat_id, "🎯 <b>目前無任何未平倉選擇權部位。</b>\n資金處於空閒狀態，可點擊「🔍 立即掃描訊號」尋找優質標的！")
            return
            
        today = datetime.date.today()
        cards = []
        total_pnl_usd = 0.0
        
        for pos in positions:
            symbol = pos["symbol"]
            opt_type = pos["option_type"]
            strike = float(pos["strike"])
            exp_date_str = pos["expiration_date"]
            premium_received = float(pos["premium_received"])
            qty = int(pos.get("quantity", 1))
            
            exp_date = datetime.datetime.strptime(exp_date_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            
            # 抓取即時標的價與選擇權最新平倉成本
            stock_price = fetch_stock_price(symbol)
            current_price, current_ask, err = fetch_option_prices(symbol, strike, exp_date_str, opt_type.lower())
            
            close_cost = current_ask if current_ask > 0 else current_price
            
            # 狀態計算
            if close_cost > 0:
                profit_pct = ((premium_received - close_cost) / premium_received) * 100
                pnl_usd = (premium_received - close_cost) * 100 * qty
                total_pnl_usd += pnl_usd
                
                # 徽章判定
                if profit_pct >= 50:
                    badge = "🎯 <b>【可停利】獲利達標</b>"
                elif profit_pct <= -50:
                    badge = "📉 <b>【警戒】浮虧擴大</b>"
                elif 0 <= dte <= 21:
                    badge = "⚠️ <b>【建議轉倉】DTE ≤ 21</b>"
                else:
                    badge = "⚡ <b>【持倉中】正常運作</b>"
                    
                pnl_color = "+" if pnl_usd >= 0 else ""
                card = (
                    f"{badge}\n"
                    f"標的: <b>${symbol}</b> (現價: ${stock_price:.2f})\n"
                    f"合約: {exp_date_str} (${strike} {opt_type.upper()}) | {qty}口\n"
                    f"收取權利金: ${premium_received:.2f} ➔ 現價: ${close_cost:.2f}\n"
                    f"損益率: <b>{profit_pct:+.1f}%</b> ({pnl_color}${pnl_usd:.1f})\n"
                    f"剩餘到期: <b>{dte} 天</b>"
                )
            else:
                card = (
                    f"⚡ <b>【持倉中】</b>\n"
                    f"標的: <b>${symbol}</b> (現價: ${stock_price:.2f})\n"
                    f"合約: {exp_date_str} (${strike} {opt_type.upper()}) | {qty}口\n"
                    f"收取權利金: ${premium_received:.2f}\n"
                    f"剩餘到期: <b>{dte} 天</b> (現價報價暫缺)"
                )
            cards.append(card)
            
        summary = (
            f"💼 <b>【活躍部位匯總清單】</b> (共 {len(positions)} 筆)\n"
            f"---------------------------------\n\n" +
            "\n\n---------------------------------\n\n".join(cards) +
            f"\n\n---------------------------------\n"
            f"💰 <b>總浮動未實現損益: {total_pnl_usd:+.2f} USD</b>"
        )
        send_reply(chat_id, summary)
    except Exception as e:
        send_reply(chat_id, f"❌ 查詢部位失敗: {e}")

def handle_history(chat_id):
    """查詢歷史已實現平倉損益"""
    try:
        history = supabase.table("trade_history").select("*").order("close_date", desc=True).execute().data
        if not history:
            send_reply(chat_id, "📊 <b>尚無任何歷史已平倉紀錄。</b>")
            return
            
        total_trades = len(history)
        win_trades = sum(1 for h in history if float(h.get("pnl", 0)) > 0)
        win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum(float(h.get("pnl", 0)) for h in history)
        
        recent_cards = []
        for h in history[:5]:
            sym = h.get("symbol")
            opt_t = h.get("option_type", "").upper()
            strike = h.get("strike")
            pnl = float(h.get("pnl", 0))
            c_date = h.get("close_date", "")
            icon = "🟢" if pnl >= 0 else "🔴"
            recent_cards.append(f"{icon} {c_date} | <b>{sym}</b> {strike} {opt_t}: <b>{pnl:+.1f} USD</b>")
            
        msg = (
            f"📊 <b>【選擇權歷史績效統計】</b>\n"
            f"---------------------------------\n"
            f"累積平倉次數: <b>{total_trades} 筆</b>\n"
            f"勝率: <b>{win_rate:.1f}%</b> ({win_trades} 勝 / {total_trades - win_trades} 負)\n"
            f"累積實現總利潤: <b>{total_pnl:+.2f} USD</b>\n"
            f"---------------------------------\n"
            f"<b>最近 5 筆平倉紀錄:</b>\n" +
            "\n".join(recent_cards)
        )
        send_reply(chat_id, msg)
    except Exception as e:
        send_reply(chat_id, f"❌ 查詢歷史損益失敗: {e}")

def handle_watchlist(chat_id):
    """查詢觀察清單即時狀態"""
    send_reply(chat_id, "⏳ 正在更新觀察清單中所有標的的 RSI 與 IV Rank...")
    try:
        rows = supabase.table("watchlist").select("*").execute().data
        if not rows:
            send_reply(chat_id, "📋 <b>目前觀察清單中沒有設定任何標的。</b>")
            return
            
        cards = []
        for r in rows:
            symbol = r["symbol"]
            price, rsi = calculate_rsi(symbol)
            iv = get_iv_rank(symbol)
            
            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            iv_str = f"{iv:.1f}%" if iv else "N/A"
            price_str = f"${price:.2f}" if price else "N/A"
            
            status = "🟢"
            if rsi and rsi < 45 and iv and iv > 30:
                status = "🚨 <b>[可開倉]</b>"
            elif rsi and rsi < 50:
                status = "🟡 [偏弱]"
                
            cards.append(f"{status} <b>${symbol}</b>: {price_str} | RSI: {rsi_str} | IV: {iv_str}")
            
        msg = (
            f"📋 <b>【觀察清單即時指標】</b>\n"
            f"---------------------------------\n" +
            "\n".join(cards) +
            f"\n---------------------------------\n"
            f"💡 <i>觸發門檻：RSI < 45 且 IV > 30% 即出現推薦合約</i>"
        )
        send_reply(chat_id, msg)
    except Exception as e:
        send_reply(chat_id, f"❌ 查詢觀察清單失敗: {e}")

def handle_ticker_check(chat_id, symbol):
    """查詢任意指定美股的指標與 Sell Put 推薦"""
    symbol = symbol.upper().strip()
    send_reply(chat_id, f"⏳ 正在體檢 <b>${symbol}</b> 即時技術面與選擇權鏈...")
    try:
        price, rsi = calculate_rsi(symbol)
        if not price:
            send_reply(chat_id, f"❌ 查無 <b>${symbol}</b> 的美股行情，請確認股票代號是否正確。")
            return
            
        iv = get_iv_rank(symbol)
        earnings = get_next_earnings(symbol)
        earn_text = f"\n⚠️ 下次財報日: <b>{earnings}</b>" if earnings else ""
        
        # 抓取推薦 Sell Put (30~45 DTE, Delta 0.15~0.25)
        options = get_real_put_option(symbol, price, 0.30, False, earnings)
        
        opt_text = ""
        if options:
            for opt in options[:3]:
                opt_text += (
                    f"👉 <b>{opt['dte']}天後到期 ${opt['strike']} Put</b>\n"
                    f"    Delta: {opt['delta']} | 權利金: ${opt['premium']} | <b>年化: {opt['annualized_return']}%</b>\n"
                )
        else:
            opt_text = "目前無符合 30~45 DTE 且流動性良好之 Put 合約。\n"
            
        msg = (
            f"🔎 <b>【${symbol} 標的即時體檢】</b>\n"
            f"現價: <b>${price:.2f}</b>\n"
            f"14日 RSI: <b>{rsi:.1f}</b>\n"
            f"IV Rank (30D): <b>{iv:.1f}%</b>{earn_text}\n"
            f"---------------------------------\n"
            f"🎯 <b>推薦 Sell Put 合約:</b>\n{opt_text}"
            f"---------------------------------\n"
            f"💡 <i>回覆任何股票代碼 (例: TSLA, NVDA) 即可隨時體檢！</i>"
        )
        send_reply(chat_id, msg)
    except Exception as e:
        send_reply(chat_id, f"❌ 查詢標的 {symbol} 失敗: {e}")

def handle_help(chat_id):
    """說明指令與鍵盤導航"""
    msg = (
        "🤖 <b>【Wheel 策略機器人使用指南】</b>\n\n"
        "你可以隨時點擊下方鍵盤按鈕，或發送下列文字/指令進行互動：\n\n"
        "🔍 <b>/scan</b> 或 <b>立即掃描</b>\n"
        "➔ 即刻以最新即時數據掃描觀察清單，尋找符合 Sell Put / CC 的黃金機會。\n\n"
        "🎯 <b>/pos</b> 或 <b>我的部位</b>\n"
        "➔ 檢視目前所有持倉的選擇權損益率%、Greeks 與是否達到 50% 停利點。\n\n"
        "📊 <b>/hist</b> 或 <b>歷史損益</b>\n"
        "➔ 統計總平倉次數、整體策略勝率與累積美元淨利。\n\n"
        "📋 <b>/watch</b> 或 <b>觀察清單</b>\n"
        "➔ 列出監控標的的即時股價、RSI 與 IV Rank。\n\n"
        "💡 <b>直接發送股票代號 (例: TSLA, AAPL, SOXL)</b>\n"
        "➔ 機器人將秒回該標的技術指標與精選 30~45 DTE 合約！\n\n"
        "⏰ <b>自動定時推播</b>：\n"
        "機器人會在週一至週五美股開盤後（冬令 <b>22:35</b> 與 <b>23:35</b>）準時主動推播！"
    )
    send_reply(chat_id, msg)

# ==========================================
# 訊息派發路由器
# ==========================================
def process_message(msg):
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", "")).strip()
    text = (msg.get("text") or "").strip()
    
    if not text:
        return
        
    # 安全驗證：僅響應授權的使用者 CHAT_ID
    if chat_id != CHAT_ID:
        print(f"⚠️ 攔截到未獲授權的私訊請求 (Chat ID: {chat_id})")
        send_reply(chat_id, "⛔ 未獲授權的帳戶。此機器人為專屬私有策略工具。")
        return
        
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 收到使用者指令: {text}")
    
    cmd = text.lower().replace("@wangwheelbot", "").strip()
    
    if cmd in ["/start", "/help", "ℹ️ 使用說明 / 指令", "說明", "幫助"]:
        handle_help(chat_id)
    elif cmd in ["/scan", "🔍 立即掃描訊號", "立即掃描", "掃描", "scan"]:
        handle_scan(chat_id)
    elif cmd in ["/pos", "/status", "🎯 查詢活躍部位", "我的部位", "部位", "持倉", "pos"]:
        handle_positions(chat_id)
    elif cmd in ["/hist", "/history", "📊 歷史損益統計", "歷史損益", "歷史", "損益", "績效"]:
        handle_history(chat_id)
    elif cmd in ["/watch", "📋 觀察清單行情", "觀察清單", "清單", "行情"]:
        handle_watchlist(chat_id)
    elif cmd.startswith("/check "):
        parts = cmd.split()
        if len(parts) >= 2:
            handle_ticker_check(chat_id, parts[1])
        else:
            send_reply(chat_id, "請在 /check 後方輸入股票代號，例如：<code>/check TSLA</code>")
    else:
        # 若為 1~5 個英文字母，視為美股代號直接體檢
        cleaned_ticker = text.strip("$").strip()
        if cleaned_ticker.isalpha() and 1 <= len(cleaned_ticker) <= 5:
            handle_ticker_check(chat_id, cleaned_ticker)
        else:
            send_reply(chat_id, "抱歉，無法識別該指令。請點選下方按鈕，或輸入股票代號（例如 <code>NVDA</code>）。")

# ==========================================
# 背景定時推播執行緒 (台灣時間冬令 22:35 / 23:35)
# ==========================================
def background_scheduler_worker():
    """在背景監控時鐘，美股開盤後精準觸發自動掃描推播"""
    tw_tz = datetime.timezone(datetime.timedelta(hours=8))
    last_triggered_minute = ""
    print("⏰ [排程核心] 冬令定時推播服務已啟動 (預設監聽台灣時間週一至週五 22:35 與 23:35)...")
    
    while True:
        try:
            now_tw = datetime.datetime.now(tw_tz)
            weekday = now_tw.weekday()  # 0=週一 ... 4=週五
            time_str = now_tw.strftime("%H:%M")
            minute_key = now_tw.strftime("%Y-%m-%d %H:%M")
            
            # 美股交易日（週一至週五）
            if 0 <= weekday <= 4:
                if time_str in ["22:35", "23:35"] and minute_key != last_triggered_minute:
                    last_triggered_minute = minute_key
                    print(f"🚀 [定時觸發] 台灣時間 {time_str} 到達，自動啟動市場掃描與部位檢查！")
                    try:
                        run_scan(force=True)
                    except Exception as e:
                        print(f"定時掃描執行異常: {e}")
            
            time.sleep(25)
        except Exception as e:
            print(f"排程線程異常: {e}")
            time.sleep(30)

# ==========================================
# 長輪詢 (Long Polling) 主迴圈
# ==========================================
def main():
    print("=" * 60)
    print("🚀 Wheel 選擇權策略 Telegram 雙向互動機器人啟動中...")
    print(f"🎯 授權 Chat ID: {CHAT_ID}")
    print("=" * 60)
    
    register_bot_commands()
    
    # 啟動背景定時排程執行緒
    sched_thread = threading.Thread(target=background_scheduler_worker, daemon=True)
    sched_thread.start()
    
    # 發送上線就緒訊息至 Telegram
    tw_tz = datetime.timezone(datetime.timedelta(hours=8))
    online_time = datetime.datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    send_reply(
        CHAT_ID,
        f"🟢 <b>【Wheel 策略機器人已連線就緒】</b>\n"
        f"上線時間：{online_time} (UTC+8)\n"
        f"你可以直接點擊下方按鈕或輸入股票代碼與我互動！\n"
        f"冬令開盤推播時間：每晚 <b>22:35</b> 與 <b>23:35</b> 自動巡檢。"
    )
    
    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    print("📡 正在監聽 Telegram 訊息... (可隨時在手機發送指令互動)")
    
    while True:
        try:
            params = {"timeout": 25}
            if offset:
                params["offset"] = offset
                
            res = requests.get(url, params=params, timeout=30)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update:
                            process_message(update["message"])
            elif res.status_code == 409:
                print("⚠️ 衝突：可能有另一個機器人實例正在運行，等待 5 秒...")
                time.sleep(5)
            else:
                print(f"連線異常，HTTP {res.status_code}: {res.text}")
                time.sleep(3)
        except requests.exceptions.RequestException:
            # 網路短暫波動，安靜等待後重試
            time.sleep(3)
        except Exception as e:
            print(f"輪詢處理錯誤: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
