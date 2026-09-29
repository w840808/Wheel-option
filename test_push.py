import streamlit as st
import requests
import sys

def test_telegram_push():
    try:
        # 嘗試讀取 secrets
        token = st.secrets.get("TELEGRAM_BOT_TOKEN")
        chat_id = st.secrets.get("CHAT_ID")
        
        if not token or not chat_id:
            print("❌ 錯誤：找不到 TELEGRAM_BOT_TOKEN 或 CHAT_ID，請確認 .streamlit/secrets.toml 是否已正確設定！")
            sys.exit(1)

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": "🤖 <b>【測試推播】</b>\n\n這是一則來自 Antigravity 美股輪盤面板的測試通知！\n\n如果您收到這則訊息，代表您的 Telegram Bot 設定完全正確喔 🎉",
            "parse_mode": "HTML"
        }
        
        print("Sending test push...")
        res = requests.post(url, json=payload)
        
        if res.status_code == 200:
            print("Success! Please check your Telegram message.")
        else:
            print(f"Failed, API response: {res.text}")
            
    except Exception as e:
        print(f"Error occurred: {e}\nPlease check your .streamlit/secrets.toml format.")

if __name__ == "__main__":
    test_telegram_push()
