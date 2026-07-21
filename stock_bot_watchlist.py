import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient
import twstock
import requests
import json

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

def get_stock_name(stock_id):
    """
    智慧查股名函式：
    1. 先從 twstock 內建字典找
    2. 若找不到，自動連線至證交所/櫃買中心公開 API 即時抓取全市場（含興櫃）股名
    """
    # 1. 先從 twstock 找
    if stock_id in twstock.codes:
        return twstock.codes[stock_id].name
        
    # 2. 若 twstock 沒有，透過公開 API 即時查詢（支援興櫃與所有最新股票）
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{stock_id}.tw|otc_{stock_id}.tw|兴櫃_{stock_id}.tw"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=3)
        data = res.json()
        if data.get("msgArray") and len(data["msgArray"]) > 0:
            # 取得股票簡稱 (n 欄位)
            name = data["msgArray"][0].get("n", "")
            if name:
                return name
    except Exception as e:
        print(f"查詢股名發生錯誤: {e}")
        
    return "興櫃/其他股票"

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
                stock_name = get_stock_name(stock_id)
                reply_text += f"🔹 {stock_id} {stock_name}\n"

    # 2. 處理新增股票指令 (支援批次輸入，空白隔開，結尾 in)
    elif text.endswith("in"):
        content = text[:-2].strip()
        raw_items = content.replace(",", " ").split()
        
        if not raw_items:
            reply_text = "❌ 請輸入要加入的股票代號或名稱喔！"
        else:
            user_data = users_collection.find_one({"_id": user_id})
            current_stocks = user_data.get("stocks", []) if user_data else []
            
            success_list = []
            fail_list = []

            for item in raw_items:
                stock_id = None
                stock_name = ""

                # 如果輸入的是純數字代號
                if item.isdigit():
                    stock_id = item
                    stock_name = get_stock_name(stock_id)
                else:
                    # 如果輸入的是中文名稱，先用 twstock 反查
                    for code, obj in twstock.codes.items():
                        if obj.name == item and obj.type == '股票':
                            stock_id = code
                            stock_name = obj.name
                            break

                if stock_id:
                    if stock_id not in current_stocks:
                        current_stocks.append(stock_id)
                    success_list.append(f"{stock_id} {stock_name}")
                else:
                    fail_list.append(item)

            users_collection.update_one(
                {"_id": user_id},
                {"$set": {"stocks": current_stocks}},
                upsert=True
            )

            reply_text = ""
            if success_list:
                reply_text += "✅ 成功加入以下股票：\n" + "\n".join([f"• {s}" for s in success_list])
            if fail_list:
                reply_text += f"\n❌ 找不到以下項目：{', '.join(fail_list)}"

    if reply_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text.strip())
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
