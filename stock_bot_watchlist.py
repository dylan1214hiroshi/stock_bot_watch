import os
import time
from datetime import datetime, time as dt_time, timedelta
import zoneinfo
from urllib.parse import quote

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient
import twstock
import feedparser

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

# ==========================================
# 📰 優化版多源新聞爬蟲核心函式 (Google News RSS + 時區過濾)
# ==========================================
def fetch_stock_news(keyword, start_time, end_time):
    news_list = []
    
    try:
        encoded_keyword = quote(keyword)
        # 使用 Google News RSS 確保穩定抓取
        url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        
        tw_tz = zoneinfo.ZoneInfo("Asia/Taipei")
        count = 0
        
        for entry in feed.entries:
            # 轉換 RSS 的 UTC 時間為台灣時間
            pub_utc = datetime(*entry.published_parsed[:6], tzinfo=zoneinfo.ZoneInfo('UTC'))
            pub_tw = pub_utc.astimezone(tw_tz)
            
            # 嚴格判斷是否落在 前一天 08:30 ~ 今天 08:30
            if start_time <= pub_tw <= end_time:
                title = entry.title
                link = entry.link
                news_item = f"• {title}\n  🔗 {link}"
                
                if news_item not in news_list:
                    news_list.append(news_item)
                    count += 1
                    
            if count >= 4: # 每檔股票最多取 4 篇
                break
                
    except Exception as e:
        print(f"新聞抓取失敗 ({keyword}): {e}")

    return news_list

# ==========================================
# ⏰ 每日早報自動推播路由 (供外部定時工具呼叫)
# ==========================================
@app.route("/morning_report", methods=["GET"])
def morning_report():
    try:
        # 1. 計算時間邊界 (前一天 08:30 ~ 當下或今天 08:30)
        tw_tz = zoneinfo.ZoneInfo("Asia/Taipei")
        now = datetime.now(tw_tz)
        
        end_time = datetime.combine(now.date(), dt_time(8, 30), tzinfo=tw_tz)
        if now < end_time:
            end_time = datetime.combine(now.date(), dt_time(8, 30), tzinfo=tw_tz) - timedelta(days=1)
        start_time = end_time - timedelta(days=1)
        
        # 2. 開始撈取資料庫用戶推播
        all_users = users_collection.find()
        count = 0
        for user in all_users:
            user_id = user.get("_id")
            stocks = user.get("stocks", [])
            if not stocks:
                continue
            
            report_content = f"☀️【早安！台股監測多空晨報】\n搜尋區間：{start_time.strftime('%m/%d %H:%M')} ~ {end_time.strftime('%m/%d %H:%M')}\n為你整理今日關注股票的最新動態：\n"
            
            for stock_id in stocks:
                stock_name = ""
                if stock_id in twstock.codes:
                    stock_name = twstock.codes[stock_id].name
                elif stock_id in MANUAL_MAP:
                    stock_name = MANUAL_MAP[stock_id]
                else:
                    stock_name = "其他股票"
                
                report_content += f"\n📊 標的：{stock_id} {stock_name}\n"
                
                # 傳入精準的時間區間進行抓取
                news = fetch_stock_news(stock_name, start_time, end_time)
                
                # 若用名稱抓不到，改用代碼抓取
                if not news and stock_name != stock_id:
                    news = fetch_stock_news(stock_id, start_time, end_time)
                
                if news:
                    report_content += "\n".join(news) + "\n"
                else:
                    report_content += "• 近期無重大公開新聞或多空消息。\n"
                
                # 避免連續請求被 Google 阻擋
                time.sleep(1.5)

            # 主動推播訊息給使用者
            line_bot_api.push_message(user_id, TextSendMessage(text=report_content.strip()))
            count += 1

        return f"Morning report sent successfully to {count} users!", 200
    except Exception as e:
        return f"Error: {str(e)}", 500

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

    # 2. 處理新增股票指令 (結尾是 in)
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

                if item.isdigit():
                    stock_id = item
                    if stock_id in twstock.codes:
                        stock_name = twstock.codes[stock_id].name
                    elif stock_id in MANUAL_MAP:
                        stock_name = MANUAL_MAP[stock_id]
                    else:
                        stock_name = "其他股票"
                else:
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

    # 3. 處理刪除股票指令 (結尾是 out)
    elif text.endswith("out"):
        content = text[:-3].strip()
        raw_items = content.replace(",", " ").split()
        
        if not raw_items:
            reply_text = "❌ 請輸入要刪除的股票代號或名稱喔！"
        else:
            user_data = users_collection.find_one({"_id": user_id})
            current_stocks = user_data.get("stocks", []) if user_data else []
            
            success_list = []
            fail_list = []

            for item in raw_items:
                stock_id = None
                stock_name = ""

                if item.isdigit():
                    stock_id = item
                    if stock_id in twstock.codes:
                        stock_name = twstock.codes[stock_id].name
                    elif stock_id in MANUAL_MAP:
                        stock_name = MANUAL_MAP[stock_id]
                    else:
                        stock_name = "其他股票"
                else:
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

                if stock_id and stock_id in current_stocks:
                    current_stocks.remove(stock_id)
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
                reply_text += "🗑️ 成功從清單移除以下股票：\n" + "\n".join([f"• {s}" for s in success_list])
            if fail_list:
                reply_text += f"\n❌ 清單中找不到以下項目：{', '.join(fail_list)}"

    # 若有產生回覆內容則傳送給使用者
    if reply_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text.strip())
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
