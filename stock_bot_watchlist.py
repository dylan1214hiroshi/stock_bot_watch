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

# 手動對應字典 (處理 twstock 抓不到的興櫃股或自訂名稱)
MANUAL_MAP = {
    "7911": "阿波羅電力",
    "7856": "漢測"
}

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
                if stock_id in twstock.codes:
                    stock_name = twstock.codes[stock_id].name
                elif stock_id in MANUAL_MAP:
                    stock_name = MANUAL_MAP[stock_id]
                reply_text += f"🔹 {stock_id} {stock_name}\n"

    # 2. 處理新增股票指令 (結尾是 in，支援單檔或多檔空白隔開)
    elif text.endswith("in"):
        content = text[:-2].strip() # 拿掉結尾的 in
        # 用空格把輸入的內容拆開成清單 (例如 ["2330", "4114", "7911"])
        raw_items = content.replace(",", " ").split()
        
        if not raw_items:
            reply_text = "❌ 請輸入要加入的股票代號或名稱喔！"
        else:
            # 讀取現有名單
            user_data = users_collection.find_one({"_id": user_id})
            current_stocks = user_data.get("stocks", []) if user_data else []
            
            success_list = []
            fail_list = []

            for item in raw_items:
                stock_id = None
                stock_name = ""

                # 判斷是代號還是中文名稱
                if item.isdigit():
                    stock_id = item
                    if stock_id in twstock.codes:
                        stock_name = twstock.codes[stock_id].name
                    elif stock_id in MANUAL_MAP:
                        stock_name = MANUAL_MAP[stock_id]
                    else:
                        stock_name = "興櫃/其他股票"
                else:
                    # 如果輸入的是中文名稱
                    reverse_map = {v: k for k, v in MANUAL_MAP.items()}
                    if item in reverse_map:
                        stock_id = reverse_map[item]
                        stock_name = item
                    else:
                        for code, obj in twstock.codes.items():
                            if obj.name == item and obj.type == '股票':
                                stock_id = code
                                stock_name = obj.name
                                break

                # 成功辨識出代號，加入暫存清單與資料庫
                if stock_id:
                    if stock_id not in current_stocks:
                        current_stocks.append(stock_id)
                    success_list.append(f"{stock_id} {stock_name}")
                else:
                    fail_list.append(item)

            # 更新資料庫
            users_collection.update_one(
                {"_id": user_id},
                {"$set": {"stocks": current_stocks}},
                upsert=True
            )

            # 組合回覆訊息
            reply_text = ""
            if success_list:
                reply_text += "✅ 成功加入以下股票：\n" + "\n".join([f"• {s}" for s in success_list])
            if fail_list:
                reply_text += f"\n❌ 找不到以下項目：{', '.join(fail_list)}"

    # 若有產生回覆內容則傳送給使用者
    if reply_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text.strip())
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
