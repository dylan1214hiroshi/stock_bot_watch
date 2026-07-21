import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient
import twstock

app = Flask(__name__)

# 從環境變數讀取 LINE 與 MongoDB 設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MONGO_URI = os.getenv("MONGO_URI")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 連線 MongoDB 資料庫
client = MongoClient(MONGO_URI)
db = client["stock_db"]
users_collection = db["users"]

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_text = ""

    # 1. 處理查看清單指令 (list)
    if text.lower() == "list":
        user_data = users_collection.find_one({"_id": user_id})
        current_stocks = user_data.get("stocks", []) if user_data else []

        if not current_stocks:
            reply_text = "你的監測名單目前是空的喔！"
        else:
            reply_text = "📊【你的專屬監測名單】\n"
            for stock_id in current_stocks:
                stock_name = ""
                # 利用 twstock 快速帶出股票名稱
                if stock_id in twstock.codes:
                    stock_name = twstock.codes[stock_id].name
                reply_text += f"🔹 {stock_id} {stock_name}\n"

    # 2. 處理新增股票指令 (結尾是 in，例如：2330 in 或 7911 in 或 台積電 in)
    elif text.endswith("in"):
        target_name_or_id = text[:-2].strip()
        stock_id = None
        stock_name = ""

        # A：判斷是不是純數字代號 (支援 2330 上市櫃，也支援 7911 興櫃或其他代號)
        if target_name_or_id.isdigit():
            stock_id = target_name_or_id
            if stock_id in twstock.codes:
                stock_name = twstock.codes[stock_id].name
            else:
                stock_name = "興櫃/其他股票"

        else:
            # B：輸入中文名稱反查代號 (例如 台積電)
            for code, obj in twstock.codes.items():
                if obj.name == target_name_or_id and obj.type == '股票':
                    stock_id = code
                    stock_name = obj.name
                    break

        if stock_id:
            # 讀取現有名單，避免重複新增
            user_data = users_collection.find_one({"_id": user_id})
            current_stocks = user_data.get("stocks", []) if user_data else []

            if stock_id not in current_stocks:
                current_stocks.append(stock_id)
                users_collection.update_one(
                    {"_id": user_id},
                    {"$set": {"stocks": current_stocks}},
                    upsert=True
                )
            reply_text = f"✅ 已將【{stock_id} {stock_name}】加入你的監測名單！"
        else:
            reply_text = f"❌ 找不到名稱為「{target_name_or_id}」的股票，請重新確認喔！"

    # 若有產生回覆內容則傳送給使用者
    if reply_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text.strip())
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
