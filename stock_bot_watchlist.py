import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from pymongo import MongoClient
import twstock
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

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
# 📰 多源新聞爬蟲核心函式 (Google 新聞 + 鉅亨網)
# ==========================================
def fetch_stock_news(keyword):
    news_list = []
    
    # 1. 抓取 Google 新聞
    try:
        encoded_keyword = quote(keyword)
        url = f"https://news.google.com/search?q={encoded_keyword}%20股票&hl=zh-TW&gl=TW&ceid=TW%3Azh-Hant"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            articles = soup.find_all("article", limit=2) # 每檔股票取前 2 篇
            for art in articles:
                title_elem = art.find("a")
                if title_elem:
                    title = title_elem.text
                    link = "https://news.google.com" + title_elem["href"][1:] if title_elem["href"].startswith(".") else title_elem["href"]
                    news_list.append(f"• {title}\n  🔗 {link}")
    except Exception as e:
        print(f"Google 新聞抓取失敗 ({keyword}): {e}")

    # 2. 抓取鉅亨網 (Anue) 搜尋結果
    try:
        encoded_keyword = quote(keyword)
        url = f"https://news.cnyes.com/news/search?q={encoded_keyword}"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            # 尋找鉅亨網新聞標題連結
            links = soup.find_all("a", href=True)
            count = 0
            for l in links:
                href = l["href"]
                if "/news/id/" in href and len(l.text.strip()) > 5:
                    title = l.text.strip()
                    full_link = f"https://news.cnyes.com{href}" if href.startswith("/") else href
                    if f"• {title}" not in news_list:
                        news_list.append(f"• {title}\n  🔗 {full_link}")
                        count += 1
                        if count >= 1: # 鉅亨網取 1 篇
                            break
    except Exception as e:
        print(f"鉅亨網抓取失敗 ({keyword}): {e}")

    return news_list[:3] # 總共合併取前 3 則

# ==========================================
# ⏰ 每日早報自動推播路由 (供外部定時工具呼叫)
# ==========================================
@app.route("/morning_report", methods=["GET"])
def morning_report():
    try:
        all_users = users_collection.find()
        count = 0
        for user in all_users:
            user_id = user.get("_id")
            stocks = user.get("stocks", [])
            if not stocks:
                continue
            
            report_content = "☀️【早安！台股監測多空晨報】\n為你整理今日關注股票的最新動態：\n"
            
            for stock_id in stocks:
                stock_name = ""
                if stock_id in twstock.codes:
                    stock_name = twstock.codes[stock_id].name
                elif stock_id in MANUAL_MAP:
                    stock_name = MANUAL_MAP[stock_id]
                else:
                    stock_name = "其他股票"
                
                report_content += f"\n📊 標的：{stock_id} {stock_name}\n"
                
                # 同時以代號與名稱搜尋新聞
                news = fetch_stock_news(stock_name) or fetch_stock_news(stock_id)
                if news:
                    report_content += "\n".join(news) + "\n"
                else:
                    report_content += "• 近期無重大公開新聞或多空消息。\n"

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
