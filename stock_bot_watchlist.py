import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient
import twstock

# 匯入我們寫好的 AI 晨報模組
from morning_report import send_morning_reports

app = Flask(__name__)

# ---------------------------------------------------------
# 環境變數與初始化
# ---------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MONGO_URI = os.getenv("MONGO_URI")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 連接 MongoDB
client = MongoClient(MONGO_URI)
db = client["stock_db"]
users_collection = db["users"]

# ---------------------------------------------------------
# 1. 測試觸發晨報的網頁路由 (會呼叫 morning_report.py)
# ---------------------------------------------------------
@app.route('/test-report', methods=['GET'])
def test_report():
    try:
        print("WEB DEBUG: 收到網頁觸發 /test-report，準備執行晨報任務...")
        send_morning_reports()
        return "✅ 晨報觸發成功！請去 LINE 查看你的專屬 AI 財經報告。"
    except Exception as e:
        print(f"❌ 觸發晨報發生錯誤: {e}")
        return f"❌ 觸發晨報時發生錯誤: {str(e)}"

# ---------------------------------------------------------
# 2. LINE Webhook 接收與指令處理
# ---------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.text.strip()
    parts = text.split()

    if len(parts) == 2:
        stock_id, action = parts[0], parts[1].lower()
        
        if action == "in":
            # 檢查股票代號是否存在
            stock_name = ""
            if stock_id in twstock.codes:
                stock_name = twstock.codes[stock_id].name
            elif stock_id == "7911":
                stock_name = "阿波羅電力"
            elif stock_id == "7856":
                stock_name = "漢測"
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"❌ 找不到股票代號：{stock_id}，請確認後再試。")
                )
                return

            # 更新 MongoDB 監聽清單
            users_collection.update_one(
                {"_id": user_id},
                {"$addToSet": {"stocks": stock_id}},
                upsert=True
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"✅ 成功加入監測：{stock_id} {stock_name}")
            )
            return

        elif action == "out":
            users_collection.update_one(
                {"_id": user_id},
                {"$pull": {"stocks": stock_id}}
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"🗑️ 已從監測清單移除：{stock_id}")
            )
            return

    elif text.lower() == "list":
        user_data = users_collection.find_one({"_id": user_id})
        if user_data and "stocks" in user_data and user_data["stocks"]:
            stocks = user_data["stocks"]
            msg = "📋 你的目前監測清單：\n" + "\n".join([f"• {s}" for s in stocks])
        else:
            msg = "📋 你的監測名單目前是空的喔！\n請輸入「代號 in」來新增（例如：2330 in）。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=msg)
        )
        return

    # 預設回覆
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="💡 指令說明：\n• 新增監測：輸入「2330 in」\n• 移除監測：輸入「2330 out」\n• 查看清單：輸入「list」")
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
