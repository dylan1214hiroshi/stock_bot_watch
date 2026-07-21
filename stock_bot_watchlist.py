import os
import requests
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient

app = Flask(__name__)

# ================= 1. 讀取環境變數 (保護金鑰安全) =================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '你的_Channel_Access_Token')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '你的_Channel_Secret')
MONGO_URI = os.environ.get('MONGO_URI', '你的_MongoDB_連線字串')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ================= 2. 初始化 MongoDB 資料庫 =================
client = MongoClient(MONGO_URI)
db = client['stock_bot_db']         
users_collection = db['users']      

def get_user_stocks(user_id):
    """從資料庫讀取特定使用者的監測名單"""
    user_data = users_collection.find_one({"_id": user_id})
    return user_data['stocks'] if user_data else []

def save_user_stocks(user_id, stocks):
    """將監測名單更新到資料庫"""
    users_collection.update_one(
        {"_id": user_id},
        {"$set": {"stocks": stocks}},
        upsert=True
    )

def get_all_users_stocks():
    """取得所有用戶的監測名單 (給每天早上 08:30 盤前匯報使用)"""
    return {user['_id']: user['stocks'] for user in users_collection.find()}


# ================= 3. LINE 訊息接收與指令邏輯 =================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text.strip().lower()
    user_id = event.source.user_id 
    
    # 從資料庫撈出該用戶目前的監測名單
    current_stocks = get_user_stocks(user_id)
    reply_text = ""

    # 查詢監測名單 (指令改為 list)
    if user_message == "list":
        if not current_stocks:
            reply_text = "你目前沒有任何監測中的股票喔！\n可以輸入「2330 in」或「台積電 in」來加入。"
        else:
            reply_text = "📊 【你的專屬監測名單】\n" + "\n".join([f"🔹 {s}" for s in current_stocks])

    # 加入監測 (股名或代碼 in)
    elif user_message.endswith("in"):
        stock_name = user_message[:-2].strip()
        if stock_name:
            if stock_name not in current_stocks:
                current_stocks.append(stock_name)
                save_user_stocks(user_id, current_stocks) 
                reply_text = f"✅ 已將【{stock_name}】加入你的監測名單！"
            else:
                reply_text = f"⚠️ 【{stock_name}】已經在你的監測名單中囉。"

    # 移除監測 (股名或代碼 out)
    elif user_message.endswith("out"):
        stock_name = user_message[:-3].strip()
        if stock_name in current_stocks:
            current_stocks.remove(stock_name)
            save_user_stocks(user_id, current_stocks) 
            reply_text = f"🗑️ 已將【{stock_name}】從你的監測名單移除。"
        else:
            reply_text = f"⚠️ 你的監測名單內找不到【{stock_name}】喔。"

    if reply_text:
         line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


# ================= 4. 盤前新聞匯總 (外部鬧鐘觸發) =================

def fetch_yesterday_news(stock):
    """模擬抓取新聞邏輯"""
    return f"昨日重點：營收表現亮眼，外資持續買超。"

@app.route("/trigger_morning_report", methods=['GET'])
def trigger_morning_report():
    """接收外部 cron-job.org 觸發"""
    secret = request.args.get('secret')
    if secret != "mypassword123":
        return "密碼錯誤", 401

    if datetime.now().weekday() >= 5:
        return "今天是週末，不發送推播。", 200

    date_str = datetime.now().strftime('%Y-%m-%d')
    all_users = get_all_users_stocks() 
    
    for user_id, stocks in all_users.items():
        if not stocks: 
            continue
            
        # 標題改為「監測名單」
        summary = f"\n📊 【盤前監測名單匯總 - {date_str}】\n\n"
        for stock in stocks:
             news = fetch_yesterday_news(stock)
             summary += f"🔹 {stock}: {news}\n"
             
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=summary))
        except Exception as e:
            print(f"發送失敗給 {user_id}: {e}")
            
    return "盤前匯總發送完畢", 200


# ================= 5. 啟動伺服器 =================
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
